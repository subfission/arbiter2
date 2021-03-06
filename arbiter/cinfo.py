#!/usr/bin/env python3

"""
A module that gets utility information relating to systemd cgroups (Linux
cgroups v1). The classes follow the usage module philosophy of Static,
non-Static and Instance. See usage.py for details.
"""

import logging
import re
import time
import pathlib
import pwd
import glob
import usage

logger = logging.getLogger("arbiter." + __name__)


def threads_per_core():
    """
    Returns the number of threads per core. Includes hyperthreading as a
    separate thread from the CPU core.
    """
    get_values = [r"siblings.+:\s(\d+)", r"cpu\scores.+:\s(\d+)"]
    cpu_values = []
    for line in open("/proc/cpuinfo"):
        if len(cpu_values) >= len(get_values):
            break
        for match in re.finditer(get_values[len(cpu_values)], str(line)):
            cpu_values.append(int(match.group(1)))
    return int(cpu_values[0] / cpu_values[1])


def proc_meminfo(mproperty=None):
    """
    Returns a string containing /proc/meminfo. If mproperty is not None,
    /proc/meminfo returns the int value after the mproperty in /proc/meminfo
    (Typically in kB). Raises ValueError if /proc/meminfo doesn't contain the
    property.

    >>> proc_meminfo("MemTotal")
    8043084
    """
    with open("/proc/meminfo") as mem_file:
        memfile = mem_file.read()
    if not mproperty:
        return memfile
    matched = re.search(r"{}:\s+(\d+)".format(mproperty), memfile)
    if matched:
        return int(matched.groups()[0])
    else:
        raise ValueError("/proc/meminfo does not contain {}".format(
            mproperty))


# Total Memory in bytes
total_mem = proc_meminfo("MemTotal") * 1024

# Total Swap size in bytes
total_swap = proc_meminfo("SwapTotal") * 1024

# Threads per core (Includes hyperthreading as a thread per core)
threads_per_core = threads_per_core()


def bytes_to_gb(memory_bytes):
    """
    Returns the memory in GB from bytes.

    memory_bytes: float, int
        The memory to convert in bytes.
    """
    return memory_bytes / 1024**3


def bytes_to_pct(memory_bytes):
    """
    Returns the memory in bytes to a percent of the machine.

    memory_bytes: float, int
        The memory to convert in bytes.
    """
    return memory_bytes / total_mem * 100


def pct_to_gb(memory_pct):
    """
    Given a percent of the machine, return how much memory in GB that is.

    memory_pct: float, int
        The memory (as a percentage of the machine e.g. 50) to convert.
    """
    return memory_pct / 100 * bytes_to_gb(total_mem)


def free_swap():
    """
    Returns the free swap size in bytes.
    """
    return proc_meminfo("SwapFree") * 1024


class StaticSystemdCGroup(usage.Usage):
    """
    A single state of a systemd cgroup that contains human readable values.
    """

    def __init__(self, name, parent, **kwargs):
        """
        Initializes a static SystemdCGroup.
        """
        super().__init__(**kwargs)
        self.name = name
        self.parent = parent
        self.base_path = pathlib.Path("/sys/fs/cgroup/")
        self._pids = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        return "<{}: {}>".format(
            type(self).__name__,
            str(self.base_path / self.parent / self.name)
        )

    def __add__(self, other):
        """
        Adds two StaticSystemdCGroup objects together by taking the values of
        the first and adding the usage and pids.
        """
        if isinstance(other, type(self)):
            new = super().__add__(other)
            new.name = self.name
            new.parent = self.parent
            new.base_path = self.base_path
            new._pids = list(set(self._pids + other._pids))
            return new
        return super().__add__(other)

    def __sub__(self, other):
        """
        Subtracts two StaticSystemdCGroup objects together by taking the values
        of the first, subtracting the usage and adding the pids.
        """
        if isinstance(other, type(self)):
            new = super().__sub__(other)
            new.name = self.name
            new.parent = self.parent
            new.base_path = self.base_path
            new._pids = list(set(self._pids + other._pids))
            return new
        return super().__sub__(other)

    def pids(self):
        """
        Returns the recorded pids of the systemd cgroup.
        """
        return self._pids


class SystemdCGroup(StaticSystemdCGroup):
    """
    An object that contains methods/properties related to a specific systemd
    cgroup.
    """

    def __init__(self, name, parent, memsw=True):
        """
        Initializes an object that contains methods/properties related to a
        systemd cgroup. If a parent is specified, the parent's path must be at
        the top level of the cgroup hierarchy (/sys/fs/cgroup). e.g. user.slice
        or system.slice.

        name: str
            The full qualified name of the cgroup. e.g. user-1000.slice,
            user.slice & systemd-journald.service.
        parent: str
            The parent of the cgroup. e.g. /, user.slice, system.slice.
        memsw: bool
            Whether or not to use memsw for cgroup memory information.

        >>> c.SystemdCGroup("systemd-journald.service", "system.slice")
        >>> c.SystemdCGroup("user.slice")
        """
        self.memsw = memsw
        super().__init__(name=name, parent=parent)

    def controller_exists(self, controller, isdir=True):
        """
        Returns whether a specific cgroup controller exists.
        """
        path = self.base_path / controller / self.parent / self.name
        return path.exists() and path.is_dir() if isdir else True

    def controller_path(self, controller="systemd", cgfile=""):
        """
        Returns the path to a cgroup property or file. If a property is not
        given, the fully qualified systemd controller path is returned. If the
        path does not exist, a FileNotFoundError is raised.

        controller: str
            A cgroup controller. e.g. cpuacct, memory, blkio
        cgfile: str
            The cgroup file below the property in the path.

        >>> SystemdCGroup("user-0.slice", "user.slice").controller_path()
        "/sys/fs/cgroup/systemd/user.slice/user-0.slice"
        >>> SystemdCGroup("user-562.slice", "user.slice").controller_path("cpuacct")
        "/sys/fs/cgroup/cpuacct/user.slice/user-562.slice"
        >>> SystemdCGroup("nota.slice", "system.slice").controller_path("memory")
        FileNotFoundError("Cgroup property...")
        >>> SystemdCGroup("user-0.slice", "user.slice").controller_path("cpuacct", "cpu.stat")
        "/sys/fs/cgroup/cpu/user.slice/user-0.slice/cpu.stat"
        """
        path = self.base_path / controller / self.parent / self.name / cgfile
        if not path.exists():
            raise FileNotFoundError("cgroup property path doesn't exist. "
                                    "This might be due to cgroup accounting "
                                    "not on, the cgroup not existing, or an "
                                    "invalid property. Path: " + str(path))
        return str(path)

    def active(self):
        """
        Returns whether the current cgroup exists in the cgroup hierarchy.

        >>> SystemdCGroup("notacgroup.slice", "user.slice").active()
        False
        """
        try:
            self.controller_path()
            return True
        except FileNotFoundError:
            return False

    def cpu_usage_per_core(self):
        """
        Returns a list of current CPU usages (in CPU time (nanosecond)) of the
        cgroup, indexed by core - 1.

        >>> self.get_cpu_usage_per_core()
        [549135244092, 1150535824026, 412981314604, 1081336776345]
        """
        prop = "cpuacct.usage_percpu"
        with open(self.controller_path("cpuacct", prop)) as cpuacct:
            return list(map(int, cpuacct.readline().split()))

    def mem_usage(self, memsw=True, kmem=False):
        """
        Gets the memory utilization as a proportion of the system's total
        memory or in bytes. If memsw is True, the swap usage is added into the
        reported memory usage.

        memsw: bool
            Whether or not to use memsw for calculating memory usage.
        kmem: bool
            Whether to include kernel memory.

        >>> self.mem_usage()
        40
        """
        filename = "memory{}.usage_in_bytes"
        usage_in_bytes = filename.format(".memsw" if memsw else "")
        mem_usage = 0
        with open(self.controller_path("memory", usage_in_bytes)) as memfile:
            mem_usage = int(memfile.read().strip())

        if not kmem:
            kmem_usage_in_bytes = filename.format(".kmem")
            with open(self.controller_path("memory", kmem_usage_in_bytes)) as memfile:
                mem_usage -= int(memfile.read().strip())
        return mem_usage

    def pids(self):
        """
        Returns a list of current pids in the cgroup.
        """
        pids = []
        with open(self.controller_path("systemd", "cgroup.procs")) as procfile:
            for pid in procfile.readlines():
                pids.append(pid.strip())
        return pids

    def cpu_quota(self):
        """
        Returns the current cgroup's CPU quota as a percentage. A -1 indicates
        that the quota has not been set.
        """
        with open(self.controller_path("cpuacct", "cpu.cfs_quota_us")) as quota:
            with open(self.controller_path("cpuacct",
                                          "cpu.cfs_period_us")) as period:
                return (float(quota.readline().strip()) /
                        float(period.readline().strip())) * 100

    def mem_quota(self, memsw=True):
        """
        Returns the current cgroup's memory quota in bytes. A -1 indicates that
        the quota has not been set.

        memsw: bool
            Whether or not to use memsw for getting the memory quota.
        """
        filename = "memory{}.limit_in_bytes".format(".memsw" if memsw else "")
        with open(self.controller_path("memory", filename)) as quota:
            return int(quota.readline().strip())

    def _set_quota(self, quota, controller, cgfile):
        """
        Writes out a cgroup quota to a file.

        quota: int
            The corresponding quota.
        controller: str
            The controller to set the quotas on. e.g. "memory", "cpuacct".
        cgfile: str
            The path to the file relative to the property. e.g. cpu.shares.
        """
        with open(self.controller_path(controller, cgfile), "w+") as prop:
            prop.write(str(quota))

    def set_mem_quota(self, quota, memsw=False):
        """
        Sets the memory quota of the user as a percentage of the machine.

        quota: float
            The memory quota (e.g. 50 for 50% of the total memory).
        memsw: bool
            Whether or not to write out to memory.memsw.limit_in_bytes.
        """
        raw_quota = int(total_mem * (quota / 100))
        files = ["memory.limit_in_bytes"]
        if memsw:
            memsw = "memory.memsw.limit_in_bytes"
            # memory.limit_in_bytes must be written before memsw, depending on
            # if higher
            if raw_quota >= self.mem_quota(memsw=True):
                files.insert(0, memsw)
            else:
                files.append(memsw)
        for memfile in files:
            self._set_quota(raw_quota, "memory", memfile)

    def set_cpu_quota(self, quota):
        """
        Sets the cpu quota of the user as a percentage of a core using the
        cpu period to get the quota relative to the shares.

        quota: float
            The cpu quota (e.g. 100 for 100% of a single core).
        """
        period_path = self.controller_path("cpuacct", "cpu.cfs_period_us")
        with open(period_path, "r") as cpu_shares_file:
            shares = int(cpu_shares_file.readline())
        self._set_quota(int(quota / 100 * shares), "cpuacct", "cpu.cfs_quota_us")


class StaticUserSlice(StaticSystemdCGroup):
    """
    A single state of a specific systemd user-$UID.slice that contains human
    readable values.
    """

    def __init__(self, uid, **kwargs):
        """
        Initializes a static UserSlice.

        uid: int
            A uid of the user-$UID.slice.
        """
        # Prevent multiple values for keyword args
        kwargs.pop("name", None)
        kwargs.pop("parent", None)
        super().__init__("user-{}.slice".format(uid), "user.slice", **kwargs)
        self.uid = int(uid)

    def __add__(self, other):
        """
        Adds two UserSlice objects together by taking the values of the first
        and adding the usage and pids.
        """
        if isinstance(other, type(self)):
            new = super().__add__(other)
            new.uid = self.uid
            return new
        return super().__add__(other)

    def __sub__(self, other):
        """
        Subtracts two UserSlice objects together by taking the values of the
        first and subtracting the usage and adding the pids.
        """
        if isinstance(other, type(self)):
            new = super().__sub__(other)
            new.uid = self.uid
            return new
        return super().__sub__(other)


class UserSlice(SystemdCGroup):
    """
    An object that contains methods/propreties related to a specific systemd
    user-$UID.slice.
    """

    def __init__(self, uid, **kwargs):
        """
        Initializes an object that contains methods/properties related to a
        systemd user-$UID.slice.

        uid: int
            A uid of the user-$UID.slice.
        """
        super().__init__("user-{}.slice".format(uid), "user.slice", **kwargs)
        self.uid = int(uid)

    def pids(self):
        """
        Returns a list of pids in the cgroup. If session@scope is turned on
        for user-$UID.slices, returns the pids in all sessions.
        """
        pids = super().pids()  # top level pids
        # if session@scope is turned on
        for session in glob.glob(self.controller_path("systemd") +
                                 "/*.scope/cgroup.procs"):
            with open(session) as proc_file:
                for pid in proc_file.readlines():
                    pids.append(pid.strip())
        return pids


class UserSliceInstance(UserSlice):
    """
    An object that contains instaneous usage information related to a specific
    systemd user-$UID.slice.
    """

    def __init__(self, uid, **kwargs):
        """
        Initializes the instaneous usage information of a systemd
        user-$UID.slice.

        uid: int
            A uid of the user-$UID.slice.
        """
        super().__init__(uid, **kwargs)
        self.time = time.time()
        # Raise errors if these fail since it likely means user disappeared
        self.cputime = sum(self.cpu_usage_per_core())
        self.memory_bytes = self.mem_usage(memsw=self.memsw)
        self._pids = self.pids()

    def __add__(self, other):
        raise TypeError(
            "Cannot add to a instaneous {} object.".format(
                type(self).__name__
            )
        )

    def __sub__(self, other):
        raise TypeError(
            "Cannot subtract to a instaneous {} object.".format(
                type(self).__name__
            )
        )

    def __truediv__(self, other):
        """
        Averages two UserSliceInstances together into a human readable
        StaticUserSlice. The divisor instance should come later in time than
        the dividend (older / newer).
        """
        if isinstance(other, type(self)):
            # If a user logs in and out between collection of instances, the
            # instances will have drastically different cpu and memory data,
            # leading to erroneous results. To get around this, we zero out
            # usage if the cputime (which is cumulative) of the first is
            # greater than the second.
            if self.cputime > other.cputime:
                calc_usage = usage.metrics.copy()  # Zero out the usage
            else:
                calc_usage = {
                    "cpu": (
                        max(other.cputime - self.cputime, 0) /
                        abs(other.time - self.time) / 1E7
                    ),
                    "mem": (
                        (other.memory_bytes + self.memory_bytes) / 2
                        / total_mem * 100
                    )
                }
            return StaticUserSlice(
                self.uid,
                _pids=list(set(self._pids + other._pids)),
                usage=calc_usage
            )
        return super().__truediv__(other)


def wait_till_uids(min_uid=0):
    """
    Blocks until users are on the machine and returns a list of uids that are
    active.
    """
    uids = []
    while not uids:
        time.sleep(0.5)
        uids = [uid for uid in current_cgroup_uids() if uid >= min_uid]
    return uids


def current_cgroup_uids(controller="memory"):
    """
    Returns a list of str uids that are active in the user.slice cgroup.

    controller: str
        Which cgroup controller to pull from. (Assumes that accounting is
        enabled for the controller)

    >>> current_cgroup_uids()
    [1000, 1001, 1003]
    """
    uids = []
    # Loop through names and remove the "user-" and ".slice" part
    for name in current_cgroups(controller=controller):
        uid = re.findall(r"-(\d+)", name)
        uids.extend(list(map(int, uid)))
    return uids


def current_cgroups(parent="user.slice", controller="systemd"):
    """
    Returns a list of all the current cgroup slices' paths that are active on
    the system. If a parent is specified, returns all cgroups below that parent
    cgroup. By default, all user cgroups are returned.

    parent: str
        The parent cgroup that contains the path of its parents.

    >>> current_cgroups()
    ["user-1000.slice", "user-1010.slice", "user-robot7.slice"]
    >>> current_cgroups("user.slice/user-1010.slice")
    []
    """
    parent.rstrip("/").lstrip("/")
    parent_path = pathlib.Path("/sys/fs/cgroup/{}/{}".format(controller, parent))

    # for each .slice in the parent path, return the slice name
    cgroup_paths = []
    for path in parent_path.glob("**/*.slice"):
        try:
            cgroup_paths.append(path.name)
        except FileNotFoundError:
            continue
    return cgroup_paths


def total_clockticks():
    """
    Returns the total cpu clock ticks of the system in jiffies.
    """
    with open("/proc/stat") as stat:
        stat_values = list(map(int, stat.readline()[5:].split(" ")))
        # Sum up the user kernel and guest time
        return sum(stat_values)


def passwd_entry(uid):
    """
    Returns whether the user can be looked up via passwd.
    """
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError as e:
        return False
