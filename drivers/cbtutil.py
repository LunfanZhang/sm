"""Utility for CBT log file operations"""
# Copyright (C) Citrix Systems Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
#
# Helper functions pertaining to VHD operations
#

import util
import uuid
from constants import CBT_UTIL
import base64


def create_cbt_log(file_name, size):
    """Create and initialise log file for tracking changed blocks"""
    cmd = [CBT_UTIL, "create", "-n", file_name, "-s", str(size)]
    _call_cbt_util(cmd)


def set_cbt_parent(file_name, parent_uuid):
    """Set parent field in log file"""
    cmd = [CBT_UTIL, "set", "-n", file_name, "-p", str(parent_uuid)]
    _call_cbt_util(cmd)


def get_cbt_parent(file_name):
    """Get parent field from log file"""
    cmd = [CBT_UTIL, "get", "-n", file_name, "-p"]
    ret = _call_cbt_util(cmd)
    uret = uuid.UUID(ret.strip())
    # TODO: Need to check for NULL UUID
    #  Ideally, we want to do
    #  if uuid.UUID(ret.strip()).int == 0
    #     return None
    #  Pylint doesn't like this for reason though
    return str(uret)


def set_cbt_child(file_name, child_uuid):
    """Set child field in log file"""
    cmd = [CBT_UTIL, "set", "-n", file_name, "-c", str(child_uuid)]
    _call_cbt_util(cmd)


def get_cbt_child(file_name):
    """Get parent field from log file"""
    cmd = [CBT_UTIL, "get", "-n", file_name, "-c"]
    ret = _call_cbt_util(cmd)
    uret = uuid.UUID(ret.strip())
    # TODO: Need to check for NULL UUID
    return str(uret)


def set_cbt_consistency(file_name, consistent):
    """Set consistency field in log file"""
    if consistent:
        flag = 1
    else:
        flag = 0
    cmd = [CBT_UTIL, "set", "-n", file_name, "-f", str(flag)]
    _call_cbt_util(cmd)


def get_cbt_consistency(file_name):
    """Get consistency field from log file"""
    cmd = [CBT_UTIL, "get", "-n", file_name, "-f"]
    ret = _call_cbt_util(cmd)
    return bool(int(ret.strip()))


def get_cbt_bitmap(file_name, base64_encoded=False):
    """Get bitmap field from log file"""
    cmd = [CBT_UTIL, "get", "-n", file_name, "-b"]
    ret = _call_cbt_util(cmd, text=base64_encoded)

    if ret and base64_encoded:
        # Decode the base64 string back to bytes
        try:
            return base64.b64decode(ret.strip())
        except Exception as e:
            util.SMlog("Failed to decode base64 bitmap data: %s" % e)
            return None

    return ret


def set_cbt_size(filename, size):
    """Set size field in log file"""
    cmd = [CBT_UTIL, "set", "-n", filename, "-s", str(size)]
    _call_cbt_util(cmd)


def get_cbt_size(file_name):
    """Get size field from log file"""
    cmd = [CBT_UTIL, "get", "-n", file_name, "-s"]
    ret = _call_cbt_util(cmd)
    return int(ret.strip())


def coalesce_bitmap(parent_path, child_path):
    """Coalesce bitmap contents of parent onto child log file"""
    cmd = [CBT_UTIL, "coalesce", "-p", parent_path, "-c", child_path]
    _call_cbt_util(cmd)


def _call_cbt_util(cmd, text=True):
    return util.ioretry(lambda: util.pread2(cmd, text=text))
