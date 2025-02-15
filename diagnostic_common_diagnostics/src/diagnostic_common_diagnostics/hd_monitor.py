#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Software License Agreement (BSD License)
#
# Copyright (c) 2009, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of the Willow Garage nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# \author Kevin Watts

from __future__ import with_statement
import rospy
import traceback
import threading
import sys
import subprocess
import socket
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

low_hd_level = 5 # TODO: make it a param
critical_hd_level = 1 # TODO: make it a param


stat_dict = { 0: 'OK', 1: 'Warning', 2: 'Error' }
usage_dict = { 0: 'OK', 1: 'Low Disk Space', 2: 'Very Low Disk Space' }


def update_status_stale(stat, last_update_time):
    time_since_update = rospy.get_time() - last_update_time

    stale_status = 'OK'
    if 20 < time_since_update <= 35:
        stale_status = 'Lagging'
        if stat.level == DiagnosticStatus.OK:
            stat.message = stale_status
        elif stat.message.find(stale_status) < 0:
            stat.message = ', '.join([stat.message, stale_status])
        stat.level = max(stat.level, DiagnosticStatus.WARN)
    if time_since_update > 35:
        stale_status = 'Stale'
        if stat.level == DiagnosticStatus.OK:
            stat.message = stale_status
        elif stat.message.find(stale_status) < 0:
            stat.message = ', '.join([stat.message, stale_status])
        stat.level = max(stat.level, DiagnosticStatus.ERROR)

    stat.values.pop(0)
    stat.values.pop(0)
    stat.values.insert(0, KeyValue(key='Update Status', value=stale_status))
    stat.values.insert(1, KeyValue(key='Time Since Update', value=str(time_since_update)))


class hd_monitor():
    def __init__(self, hostname, diag_hostname, home_dir=''):
        self._mutex = threading.Lock()

        self._hostname = hostname
        self._no_temp_warn = rospy.get_param('~no_hd_temp_warn', False)
        if self._no_temp_warn:
            rospy.logwarn('Not warning for HD temperatures is deprecated. This will be removed in D-turtle')
        self._home_dir = home_dir

        self._diag_pub = rospy.Publisher('/diagnostics', DiagnosticArray, queue_size=10)

        self._last_temp_time = 0
        self._last_usage_time = 0
        self._last_publish_time = 0

        self._temp_timer = None
        self._usage_timer = None


        if self._home_dir != '':
            self._usage_stat = DiagnosticStatus()
            self._usage_stat.level = DiagnosticStatus.ERROR
            self._usage_stat.hardware_id = hostname
            self._usage_stat.name = '%s HD Usage' % diag_hostname
            self._usage_stat.values = [KeyValue(key='Update Status', value='No Data' ),
                                       KeyValue(key='Time Since Last Update', value='N/A')]
            self.check_disk_usage()

    # Must have the lock to cancel everything
    def cancel_timers(self):
        if self._usage_timer:
            self._usage_timer.cancel()
            self._usage_timer = None

    def check_disk_usage(self):
        if rospy.is_shutdown():
            with self._mutex:
                self.cancel_timers()
            return

        diag_vals = [KeyValue(key='Update Status', value='OK'),
                     KeyValue(key='Time Since Last Update', value='0')]
        diag_level = DiagnosticStatus.OK
        diag_message = 'OK'

        try:
            p = subprocess.Popen(["df", "-P", "--block-size=1G", self._home_dir],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()
            retcode = p.returncode

            if retcode == 0:

                diag_vals.append(KeyValue(key='Disk Space Reading', value='OK'))
                row_count = 0
                for row in stdout.decode('utf-8').split('\n'):
                    if len(row.split()) < 2:
                        continue
                    if not row.split()[1].isnumeric() or float(row.split()[1]) < 10:  # Ignore small drives
                        continue

                    row_count += 1
                    g_available = row.split()[-3]
                    name = row.split()[0]
                    size = row.split()[1]
                    mount_pt = row.split()[-1]

                    if float(g_available) > low_hd_level:
                        level = DiagnosticStatus.OK
                    elif float(g_available) > critical_hd_level:
                        level = DiagnosticStatus.WARN
                    else:
                        level = DiagnosticStatus.ERROR

                    diag_vals.append(KeyValue(
                        key='Disk %d Name' % row_count, value=name))
                    diag_vals.append(KeyValue(
                        key='Disk %d Available (GiB)' % row_count, value=g_available))
                    diag_vals.append(KeyValue(
                        key='Disk %d Size (GiB)' % row_count, value=size))
                    diag_vals.append(KeyValue(
                        key='Disk %d Status' % row_count, value=stat_dict[level]))
                    diag_vals.append(KeyValue(
                        key='Disk %d Mount Point' % row_count, value=mount_pt))

                    diag_level = max(diag_level, level)
                    diag_message = usage_dict[diag_level]

            else:
                diag_vals.append(KeyValue(key='Disk Space Reading', value='Failed'))
                diag_level = DiagnosticStatus.ERROR
                diag_message = stat_dict[diag_level]

        except Exception:
            rospy.logerr(traceback.format_exc())

            diag_vals.append(KeyValue(key='Disk Space Reading', value='Exception'))
            diag_vals.append(KeyValue(key='Disk Space Ex', value=traceback.format_exc()))

            diag_level = DiagnosticStatus.ERROR
            diag_message = stat_dict[diag_level]

        # Update status
        with self._mutex:
            self._last_usage_time = rospy.get_time()
            self._usage_stat.values = diag_vals
            self._usage_stat.message = diag_message
            self._usage_stat.level = diag_level

            if not rospy.is_shutdown():
                self._usage_timer = threading.Timer(5.0, self.check_disk_usage)
                self._usage_timer.start()
            else:
                self.cancel_timers()

    def publish_stats(self):
        with self._mutex:

            msg = DiagnosticArray()
            msg.header.stamp = rospy.get_rostime()
            if self._home_dir != '':
                update_status_stale(self._usage_stat, self._last_usage_time)
                msg.status.append(self._usage_stat)

            if rospy.get_time() - self._last_publish_time > 0.5:
                self._diag_pub.publish(msg)
                self._last_publish_time = rospy.get_time()


# Todo: Need to check HD input/output too using iostat

if __name__ == '__main__':
    hostname = socket.gethostname()

    import optparse
    parser = optparse.OptionParser(usage="usage: hd_monitor.py [--diag-hostname=cX]")
    parser.add_option("--diag-hostname", dest="diag_hostname",
                      help="Computer name in diagnostics output (ex: 'c1')",
                      metavar="DIAG_HOSTNAME",
                      action="store", default = hostname)
    options, args = parser.parse_args(rospy.myargv())

    home_dir = ''
    if len(args) > 1:
        home_dir = args[1]

    hostname_clean = hostname.translate(hostname.maketrans('-', '_'))
    try:
        rospy.init_node('hd_monitor_%s' % hostname_clean)
    except rospy.exceptions.ROSInitException:
        print('HD monitor is unable to initialize node. Master may not be running.')
        sys.exit(0)

    hd_monitor = hd_monitor(hostname, options.diag_hostname, home_dir)
    rate = rospy.Rate(1.0)

    try:
        while not rospy.is_shutdown():
            rate.sleep()
            hd_monitor.publish_stats()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        traceback.print_exc()
        raise e

    hd_monitor.cancel_timers()
    sys.exit(0)



