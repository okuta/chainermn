import errno
import os
import shutil
import tempfile
import time

import chainer
from chainer.training import extension


def distributed_cpr(name, comm, cp_interval=5, gc_interval=5, path=None):
    '''Create Distributed CPR Extension

    Generational snapshot extension to allow fault tolerance;
    It keeps several old snapshots to rollback synchronized
    snapshot at each MPI process. Snapshot files are identified
    as '<name>.<rank>.<iteration>'.
    <name> ... identifier of the run where snapshot is kept for
    <rank> ... which process owned the model
    <iteration> ... number of iteration.

    This extension keeps several files for each execution and allows
    users to resume the whole job at the latest snapshots of each MPI
    process, and the iteration where all snapshots agrees.

    As this object is a usual Chainer extension, users can just
    create this object and pass to the trainer as an extension::

        cpr = distributed_cpr(name=run_id, comm=comm)
        trainer.extend(cpr, trigger=(25, 'iteration'))

    To run recovery at startup, before first iteration, run::

        cpr.maybe_resume(trainer, optimizer)

    before ``trainer.run()`` . If nothing is recovered (i.e. no
    snapshot found), ``trainer.updater.iteration`` will remain ``0``
    . Otherwise it will have the value of snapshot and the training
    will resume from that iteration. ``optimizer`` is optional but
    this will let multi node optimizer avoid initial broadcast when
    all snapshot data among nodes are all in sync.

    Another example to use CPR *without* trainer would be::

        cpr = distributed_cpr(name=run_id, comm=comm)
        cpr.maybe_resume(obj_you_want_to_cpr, optimizer)

        while True: ## Training loop
            ...
            updater.update()
            ...
            cpr.checkpoint(obj_you_want_to_cpr)  # Update checkpoints

    c.f. Taking snapshots in single node execution would be much simpler::

        trainer.extend(extensions.snapshot())

    TODO(kuenishi): do we need checksum? ... snapshot_object is smart
    that uses temporary files and then moving the file, which is
    usually an atomic operation. If we assume external (malicious or
    innocent) hands such as bit rot or file truncate we need this. In
    current implementation manual removal of latest snapshot files will
    let recovery happen against next-latest snapshot.
    TODO(kuenishi): make non-distributed version and contribute to Chainer?

    Args:
        name (str): unique id of the run
        comm: communicater in ChainerMN
        cp_interval (int): number of checkpoints to guarantee preserved
        gc_interval (int): interval to collect non-preserved checkpoints
    '''
    return _DistCPRExtension(name, comm, cp_interval, gc_interval, path)


class _CPRStats(object):
    def __init__(self):
        self.timings = []
        self.begin = None

    def start(self):
        self.begin = time.time()

    def end(self):
        e = time.time()

        if self.begin is None:
            return

        self.timings.append({'b': self.begin, 'd': e - self.begin})
        self.begin = None

    def report(self):
        count = len(self.timings)
        if count == 0:
            return 'No stats available'

        durations = [t['d'] for t in self.timings]
        average = sum(durations) / count
        fmt_str = "Snapshot duration stats (sec): avg={:f}, min={:f}, max={:f}"
        return fmt_str.format(average, min(durations), max(durations))


class _DistCPRExtension(extension.Extension):

    def __init__(self, name, comm, cp_interval, gc_interval, path):
        self.name = name
        self.cp_interval = cp_interval
        self.gc_interval = gc_interval
        self.comm = comm
        self.files = []
        self.stats = _CPRStats()

        # TODO(kuenishi): support path expression such as
        # 'path/{rank}/snapshot' or 'path/{host}/snapshot'
        if path is not None:
            self.path = path
            _maybe_makedirs(self.path)
        else:
            self.path = None

        assert name is not None
        assert self.cp_interval > 0
        assert self.gc_interval > 0
        assert self.comm is not None

    def __call__(self, trainer):
        # This is supposed to be called at the exact same interval
        # among all nodes
        if self.path is None:
            # Note: In a non-trainer use case this path will fail; You
            # shouldn't pass None at __init__().
            self.path = trainer.out

        self.checkpoint(trainer, trainer.updater.iteration)

    def checkpoint(self, target, iteration):
        filename = self._filename(iteration)

        self.stats.start()
        _save(self.path, filename, target)
        self.stats.end()

        self.files.append(filename)

        if len(self.files) - self.cp_interval > 5:
            # remove older snapshots, and bcast latest list
            self._sync_file_list(remove_remainder=True)

    def finalize(self):
        assert self.path is not None

        files2remove = self.files
        for file in files2remove:
            filename = os.path.join(self.path, file)
            try:
                os.remove(filename)
            except Exception:
                pass

        self.files = []

    def get_stats(self):
        return self.stats.report()

    def _sync_file_list(self, remove_remainder=False):
        file_lists = self.comm.mpi_comm.gather(self.files, root=0)

        iters0 = None
        if self.comm.rank == 0:
            if file_lists is not None:
                if len(file_lists) == 0:
                    self.files = []
                    return

                iters0 = set(
                    [i for _, _, i in self._parse_filenames(file_lists[0])])
                for files in file_lists[1:]:
                    iters = set(
                        [i for _, _, i in self._parse_filenames(files)])
                    iters0 &= iters

                iters0 = list(iters0)
                iters0.sort()
                iters0 = iters0[-self.cp_interval:]

            else:
                raise RuntimeError("Can't gather checkpoint file names")

        iters0 = self.comm.mpi_comm.bcast(iters0, root=0)
        files = self._filenames(iters0)

        if remove_remainder:
            files2remove = set(self.files) - set(files)
            for file in files2remove:
                try:
                    os.remove(os.path.join(self.path, file))
                except Exception:
                    pass

        self.files = files

    def _filenames(self, iterations):
        return [self._filename(i) for i in iterations]

    def _filename(self, iteration):
        # TODO(kuenishi): As a node identifier, should we use node
        # name (e.g. hostname) or MPI rank?
        #
        # hostname is fine when MPI rank changes among same set of nodes.
        # MPI rank is fine when node fails and a new node has come.
        filename = '{:s}.{:d}.{:d}'.format(
            self.name, self.comm.rank, iteration)
        return filename

    def _parse_filenames(self, filenames):
        # extract filenames and return [ <iteration> ]
        return [self._parse_filename(f) for f in filenames]

    def _parse_filename(self, filename):
        tpl = filename.split('.')
        if len(tpl) != 3:
            return
        name, rank, iter = tpl
        if name != self.name:
            return
        return name, int(rank), int(iter)

    def maybe_resume(self, trainer, optimizer=None, path=None):
        # If there's existing model, load, sync, and resume.
        if self.path is None:
            if path is not None:
                self.path = path
            else:
                self.path = trainer.out

        local_files = []
        try:
            local_files = os.listdir(self.path)
        except Exception:
            # Maybe I am the only process that does not have result
            # directory
            pass
        local_iters = filter(None, self._parse_filenames(local_files))
        local_iters = [i for name, rank, i in local_iters if name ==
                       self.name and rank == self.comm.rank]

        self.files = self._filenames(local_iters)
        # Collect common file list
        self._sync_file_list()

        # Get set of common snapshot numbers (=iteration number)
        iters = [i for name, rank, i in self._parse_filenames(self.files)]
        if len(iters) > 0:
            # Adopt latest snapshot from iteration number
            i = max(iters)

            # Note that CPR only verifies file name - if exception
            # happens here, currently manual deletion of *latest*
            # snapshot may CPR work sanely against one older snapshot
            _load(self.path, self._filename(i), trainer)

            if optimizer is not None:
                # If this is a complete resume, no broadcast is needed ^^;
                # 'complete resume' means all workers' snapshot is preserved,
                # so all workers can assume their loaded model is complete.
                # Otherwise _MultiNodeOptimizer broadcasts and shares data
                # from rank 0.
                optimizer.__setattr__('needs_broadcast', False)


def _load(path, filename, target):
    chainer.serializers.load_npz(os.path.join(path, filename), target)


def _save(path, filename, target):
    # Simple save_npz may cause partial write - instead copied and
    # modified a bit from chainer.extensions.snapshot.
    _maybe_makedirs(path)
    prefix = 'tmp-' + filename
    fd, tmppath = tempfile.mkstemp(prefix=prefix, dir=path)
    try:
        chainer.serializers.save_npz(tmppath, target)
    except Exception:
        os.close(fd)
        os.remove(tmppath)
        raise
    os.close(fd)
    shutil.move(tmppath, os.path.join(path, filename))


def _maybe_makedirs(path):
    # This is for Python 2-3 compatibility;
    # os.makedirs(path, exist_ok=True) would be more simpler
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise
