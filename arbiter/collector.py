"""
A module containing objects/methods for collecting information.
"""

import time
import logging
import collections
import user
import usage
import pidinfo
import cinfo
from cfgparser import cfg, shared

logger = logging.getLogger("arbiter." + __name__)


class Collector(object):
    """
    A object that collects information about cgroups and processes and writes
    that information to a user.User() object.
    """

    def __init__(self, repetitions, interval, poll=3, rhel7_compat=False):
        """
        Initializes the collector. Run the collector by doing self.run().

        repetitions: int
            How many times to collect information over the interval.
        interval: int
            The interval to collect between.
        poll: int
            The number of polls to average over the interval when measuring
            processes. Must be greater than 2.
        rhel7_compat: bool
            Whether or not to use rhel7 configuration for backwards
            compatability.
        """
        self.repetitions = repetitions
        self.poll = poll if poll >= 2 else 2
        self.interval = interval
        self.users = {}
        self.allusers = usage.metrics.copy()
        self.rhel7_compat = rhel7_compat

    def delete_user(self, uid):
        """
        Deletes the given user such that information is not collected on them
        until they are present on the system again.

        uid: int
            A uid of a user to remove.
        """
        del self.users[uid]

    def refresh_users(self):
        """
        Refreshes the internal users dictionary by adding new users that
        haven't been seen before.
        """
        active_uids = cinfo.current_cgroup_uids()
        self.users.update(
            {uid: user.User(uid) for uid in active_uids
             if uid >= cfg.general.min_uid
             # Not sure why, but sometimes there are users without a passwd
             # entry, which causes things to fail, ignore them here.
             and cinfo.passwd_entry(uid)
             and uid not in self.users}
        )

    def _pre_run(self):
        """
        Initializes allusers to be empty.
        """
        self.allusers = usage.metrics.copy()

    def _pre_collect(self):
        """
        Initializes the users to collect info on, with their history.
        """
        starttime = int(time.time())
        for user_obj in self.users.values():
            user_obj.history.appendleft({"time": starttime})

    def _post_run(self):
        """
        Computes the total usage from the sum of user cpu and memory.
        """
        self.allusers["cpu"] = sum(
            user_obj.cpu_usage for user_obj in self.users.values()
        )
        self.allusers["mem"] = sum(
            user_obj.mem_usage for user_obj in self.users.values()
        )

    def _post_collect(self):
        """
        Modifies the history of users to add post-collect values.
        """
        for uid, user_obj in self.users.items():
            user_history = user_obj.history[0]
            summed_proc = pidinfo.StaticProcess()
            if user_history["pids"]:  # If there are processes
                summed_proc = sum(user_history["pids"].values())

            if self.rhel7_compat:
                # Replace general memory (cgroup) with process memory
                user_history["mem"] = summed_proc.usage["mem"]

            # Add a mark to the end of all whitelisted process names
            user_obj.mark_whitelisted_processes(user_history["pids"].values())

            # Add "other processes"
            user_history["pids"][-1] = pidinfo.StaticProcess(
                usage={
                    "cpu": max(user_history["cpu"] - summed_proc.usage["cpu"], 0),
                    "mem": max(user_history["mem"] - summed_proc.usage["mem"], 0)
                },
                name=shared.other_processes_label + "**",
                owner=uid
            )

    def collect(self):
        """
        Collects information into each user's history.
        """
        self._pre_collect()
        waittime = self.interval / self.poll
        timer = TimeRecorder()

        gen_instant_histories = collections.defaultdict(list)
        processes_instant_histories = collections.defaultdict(
            lambda: collections.defaultdict(list)
        )
        for _ in range(0, self.poll):
            timer.start(waittime)

            # Collect usage in a poll
            for uid, user_obj in self.users.items():
                # Collect General Metrics: CPU, Memory (UserSliceInstance())
                pids = []
                try:
                    user_slice = cinfo.UserSliceInstance(
                        uid,
                        memsw=cfg.processes.memsw
                    )
                    pids = user_slice.pids()
                    gen_instant_histories[user_obj].append(user_slice)
                except (FileNotFoundError, OSError) as err:
                    if err.errno == 13:
                        # Cannot write to a file
                        logger.warning(err)

                # Collect Process Metrics: CPU, Memory (ProcessInstance())
                for pid in pids:
                    try:
                        processes_instant_histories[user_obj][pid].append(
                            pidinfo.ProcessInstance(pid, pss=cfg.processes.pss,
                                                    swap=cfg.processes.memsw)
                        )
                    except (FileNotFoundError, OSError) as err:
                        if err.errno == 13:
                            # Likely don't have permission to read from
                            # /proc/<pid>/smaps
                            logger.warning(err)
            time.sleep(timer.delta)

        # Average the usage over all the polls
        for user_obj in self.users.values():
            # -1 since we have one less static obj than instant obj after combo
            div_by = self.poll - 1
            # Average UserSliceInstance() into StaticUserSlice()
            if len(gen_instant_histories[user_obj]) >= 2:
                userslice = usage.average(
                    *usage.combine(*gen_instant_histories[user_obj]),
                    by=div_by
                )
                user_obj.history[0].update(userslice.usage)
            else:
                # Cannot avg a single instantaneous user slice
                user_obj.history[0].update(usage.metrics.copy())  # empty metrics

            # Average ProcessInstance() into StaticProcess()
            user_obj.history[0]["pids"] = {
                pid: usage.average(
                    *usage.combine(*processes),
                    by=div_by
                )
                for pid, processes in processes_instant_histories[user_obj].items()
                if len(processes) >= 2   # Cannot avg a single instant process
            }
        self._post_collect()

    def run(self):
        """
        Runs the collector self.repetitions times, with each collection over
        the self.interval. Returns a StaticUserSlice that is the sum of all
        the users and a dictionary of users identified by their uid.
        """
        self.refresh_users()
        self._pre_run()

        for _ in range(self.repetitions):
            self.collect()

        # Add properties like looking up statuses, quotas, etc...
        for uid, user_obj in self.users.items():
            user_obj.update_properties()

        self._post_run()
        return self.allusers, self.users


class TimeRecorder(object):
    """
    Accurately record changes in time.
    """

    def __init__(self):
        self.start_time = time.time()
        self.waittime = 0

    def start(self, waittime):
        """
        Starts the time recorder.
        """
        self.waittime = waittime
        self.start_time = time.time()

    @property
    def delta(self):
        """
        Returns how much waiting is left.
        """
        return max(0, self.waittime - self.time_since_start)

    @property
    def time_since_start(self):
        """
        Returns the amount of time since the start time.
        """
        return time.time() - self.start_time
