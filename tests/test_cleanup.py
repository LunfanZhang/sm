import errno
import signal
import unittest
import unittest.mock as mock
import uuid

from uuid import uuid4

from sm import cleanup
from sm.core import lock

from sm.core import util
from sm import vhdutil

from sm import ipc

import XenAPI


class FakeFile(object):
    pass


class FakeException(Exception):
    pass


class FakeUtil:
    record = []

    def log(input):
        FakeUtil.record.append(input)
    log = staticmethod(log)


class AlwaysLockedLock(object):
    def acquireNoblock(self):
        return False


class AlwaysFreeLock(object):
    def acquireNoblock(self):
        return True


class TestRelease(object):
    def acquireNoblock(self):
        return True


class IrrelevantLock(object):
    pass


def create_cleanup_sr(xapi, uuid=None):
    return cleanup.SR(uuid=uuid, xapi=xapi, createLock=False, force=False)


class TestSR(unittest.TestCase):
    def setUp(self):
        time_sleep_patcher = mock.patch('sm.cleanup.time.sleep')
        self.mock_time_sleep = time_sleep_patcher.start()

        updateBlockInfo_patcher = mock.patch('sm.cleanup.VDI.updateBlockInfo')
        self.mock_updateBlockInfo = updateBlockInfo_patcher.start()

        IPCflag_patcher = mock.patch('sm.cleanup.IPCFlag')
        self.mock_IPCFlag = IPCflag_patcher.start()

        blktap2_patcher = mock.patch('sm.cleanup.blktap2', autospec=True)
        self.mock_blktap2 = blktap2_patcher.start()

        self.xapi_mock = mock.MagicMock(name='MockXapi')
        self.xapi_mock.srRecord = {'name_label': 'dummy'}
        self.xapi_mock.isPluggedHere.return_value = True
        self.xapi_mock.isMaster.return_value = True
        self.mock_xapi_session = mock.MagicMock(name="MockSession")
        self.xapi_mock.getSession.return_value = self.mock_xapi_session

        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        cleanup.SIGTERM = False

    def setup_abort_flag(self, ipc_mock, should_abort=False):
        flag = mock.Mock()
        flag.test = mock.Mock(return_value=should_abort)

        ipc_mock.return_value = flag

    def setup_mock_sr(self, mock_sr):
        mock_sr.configure_mock(uuid=1234, xapi=self.xapi_mock,
                               createLock=False, force=False)

    def mock_cleanup_locks(self):
        cleanup.lockGCActive = TestRelease()
        cleanup.lockGCActive.release = mock.Mock(return_value=None)

        cleanup.lockGCRunning = TestRelease()
        cleanup.lockGCRunning.release = mock.Mock(return_value=None)

    def test_check_no_space_candidates_none(self):
        sr = create_cleanup_sr(self.xapi_mock)
        sr.xapi.srRecord.update({
            "sm_config": {}
        })

        sr.check_no_space_candidates()

        self.mock_xapi_session.xenapi.message.create.assert_not_called()

    def test_check_no_space_candidates_one_not_reported(self):
        sr = create_cleanup_sr(self.xapi_mock)
        vdi_uuid = str(uuid.uuid4())
        mock_vdi = mock.MagicMock()
        mock_vdi.uuid = vdi_uuid

        sr.no_space_candidates = {
            vdi_uuid: mock_vdi
        }
        sr.xapi.srRecord.update({
            "sm_config": {}
        })
        self.mock_xapi_session.xenapi.VDI.get_other_config.return_value = {}
        xapi_message = self.mock_xapi_session.xenapi.message
        xapi_message.get_record.side_effect = XenAPI.Failure(
            details='No such message')

        sr.check_no_space_candidates()

        self.mock_xapi_session.xenapi.message.create.assert_called_once_with(
            'SM_GC_NO_SPACE', 3, 'SR', sr.uuid,
            "Unable to perform data coalesce "
            f"due to a lack of space in SR {sr.uuid}")

    def test_check_no_space_candidates_one_already_reported(self):
        sr = create_cleanup_sr(self.xapi_mock)
        vdi_uuid = str(uuid.uuid4())
        mock_vdi = mock.MagicMock()
        mock_vdi.uuid = vdi_uuid

        sr.no_space_candidates = {
            vdi_uuid: mock_vdi
        }
        sr.xapi.srRecord.update({
            "sm_config": {"gc_no_space": "dummy ref"}
        })
        self.mock_xapi_session.xenapi.VDI.get_other_config.return_value = {}
        self.mock_xapi_session.xenapi.message.get_record.side_effect = {
            'name': 'SM_GC_NO_SPACE'
        }

        sr.check_no_space_candidates()

        self.mock_xapi_session.xenapi.message.create.assert_not_called()

    def test_check_no_space_candidates_none_clear_message(self):
        sr = create_cleanup_sr(self.xapi_mock)
        vdi_uuid = str(uuid.uuid4())
        mock_vdi = mock.MagicMock()
        mock_vdi.uuid = vdi_uuid

        sr.no_space_candidates = {}
        sr.xapi.srRecord.update({
            "sm_config": {"gc_no_space": "dummy ref"}
        })
        self.mock_xapi_session.xenapi.VDI.get_other_config.return_value = {}
        self.mock_xapi_session.xenapi.message.get_record.side_effect = {
            'name': 'SM_GC_NO_SPACE'
        }

        sr.check_no_space_candidates()

        self.mock_xapi_session.xenapi.message.destroy.assert_called_once_with(
            "dummy ref"
        )

    def test_term_handler(self):
        self.assertFalse(cleanup.SIGTERM)

        cleanup.receiveSignal(signal.SIGTERM, None)

        self.assertTrue(cleanup.SIGTERM)

    @mock.patch('sm.cleanup._create_init_file', autospec=True)
    @mock.patch('sm.cleanup.SR', autospec=True)
    def test_loop_exits_on_term(self, mock_init, mock_sr):
        # Set the term signel
        cleanup.receiveSignal(signal.SIGTERM, None)
        mock_session = mock.MagicMock(name='MockSession')
        sr_uuid = str(uuid4())
        self.mock_cleanup_locks()

        # Trigger GC
        cleanup.gc(mock_session, sr_uuid, inBackground=False)

    def test_lock_if_already_locked(self):
        """
        Given an already locked SR, a lock call
        increments the lock counter
        """

        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = IrrelevantLock()
        sr._locked = 1

        sr.lock()

        self.assertEqual(2, sr._locked)

    def test_lock_if_no_locking_is_used(self):
        """
        Given no srLock present, the lock operations don't touch
        the counter
        """

        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = None

        sr.lock()

        self.assertEqual(0, sr._locked)

    def test_lock_succeeds_if_lock_is_acquired(self):
        """
        After performing a lock, the counter equals to 1
        """

        self.setup_abort_flag(self.mock_IPCFlag)
        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = AlwaysFreeLock()

        sr.lock()

        self.assertEqual(1, sr._locked)

    def test_lock_raises_exception_if_abort_requested(self):
        """
        If IPC abort was requested, lock raises AbortException
        """

        self.setup_abort_flag(self.mock_IPCFlag, should_abort=True)
        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = AlwaysLockedLock()

        self.assertRaises(cleanup.AbortException, sr.lock)

    def test_lock_raises_exception_if_unable_to_acquire_lock(self):
        """
        If the lock is busy, SMException is raised
        """

        self.setup_abort_flag(self.mock_IPCFlag)
        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = AlwaysLockedLock()

        self.assertRaises(util.SMException, sr.lock)

    def test_lock_leaves_sr_consistent_if_unable_to_acquire_lock(self):
        """
        If the lock is busy, the lock counter is not incremented
        """

        self.setup_abort_flag(self.mock_IPCFlag)
        sr = create_cleanup_sr(self.xapi_mock)
        sr._srLock = AlwaysLockedLock()

        with self.assertRaises(util.SMException):
            sr.lock()

        self.assertEqual(0, sr._locked)

    def test_gcPause_fist_point_legal(self):
        """
        Make sure the fist point has been added to the array of legal
        fist points.
        """
        self.assertTrue(util.fistpoint.is_legal(util.GCPAUSE_FISTPOINT))

    @mock.patch('sm.cleanup.util.fistpoint', autospec=True)
    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    def test_gcPause_calls_fist_point(
            self,
            mock_abortable,
            mock_sr,
            mock_fist):
        """
        Call fist point if active and not abortable sleep.
        """
        self.setup_mock_sr(mock_sr)

        # Fake that we have an active fist point.
        mock_fist.is_active.return_value = True

        cleanup._gcLoopPause(mock_sr, False)

        # Make sure we check for correct fist point.
        mock_fist.is_active.assert_called_with(util.GCPAUSE_FISTPOINT)

        # Make sure we are calling the fist point.
        mock_fist.activate_custom_fn.assert_called_with(util.GCPAUSE_FISTPOINT,
                                                        mock.ANY)

        # And don't call abortable sleep
        mock_abortable.assert_not_called()

    @mock.patch('sm.cleanup.util.fistpoint', autospec=True)
    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    @mock.patch('sm.cleanup.os.path.exists', autospec=True)
    def test_gcPause_calls_abortable_sleep(
            self,
            mock_exists,
            mock_abortable,
            mock_sr,
            mock_fist_point):
        """
        Call abortable sleep if fist point is not active.
        """
        self.setup_mock_sr(mock_sr)

        # Fake that the fist point is not active.
        mock_fist_point.is_active.return_value = False
        # The GC init file does exist
        mock_exists.return_value = True

        cleanup._gcLoopPause(mock_sr, False)

        # Make sure we check for the active fist point.
        mock_fist_point.is_active.assert_called_with(util.GCPAUSE_FISTPOINT)

        # Fist point is not active so call abortable sleep.
        mock_abortable.assert_called_with(mock.ANY, None, mock_sr.uuid,
                                          mock.ANY, cleanup.VDI.POLL_INTERVAL,
                                          cleanup.GCPAUSE_DEFAULT_SLEEP * 1.1)

    @mock.patch('sm.cleanup.util.fistpoint', autospec=True)
    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    @mock.patch('sm.cleanup.os.path.exists', autospec=True)
    def test_gcPause_skipped_on_first_run(
            self,
            mock_exists,
            mock_abortable,
            mock_sr,
            mock_fist_point):
        """
        Don't sleep the GC on the first run after host boot.
        """
        self.setup_mock_sr(mock_sr)

        # Fake that the fist point is not active.
        mock_fist_point.is_active.return_value = False
        # The GC init file doesn't exist
        mock_exists.return_value = False

        cleanup._gcLoopPause(mock_sr, False)

        # Make sure we check for the active fist point.
        mock_fist_point.is_active.assert_called_with(util.GCPAUSE_FISTPOINT)

        # Fist point is not active so call abortable sleep.
        self.assertEqual(0, mock_abortable.call_count)

    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    def test_gc_pause_skipped_if_immediate(self, mock_abortable, mock_sr):
        """
        Foreground GC runs immediate
        """
        ## Arrange
        self.setup_mock_sr(mock_sr)

        ## Act
        cleanup._gcLoopPause(mock_sr, False, immediate=True)

        ## Assert
        # Never call runAbortable
        self.assertEqual(0, mock_abortable.call_count)

    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup._abort')
    def test_lock_released_by_abort_when_held(
            self,
            mock_abort,
            mock_sr):
        """
        If _abort returns True make sure we release the lockGCActive which will
        have been held by _abort, also check that we return True.
        """
        self.setup_mock_sr(mock_sr)

        # Fake that abort returns True, so we hold lockGCActive.
        mock_abort.return_value = True

        # Setup mock of release function.
        cleanup.lockGCActive = TestRelease()
        cleanup.lockGCActive.release = mock.Mock(return_value=None)

        ret = cleanup.abort(mock_sr, False)

        # Pass on the return from _abort.
        self.assertEqual(True, ret)

        # We hold lockGCActive so make sure we release it.
        self.assertEqual(cleanup.lockGCActive.release.call_count, 1)

    @mock.patch('sm.cleanup.SR', autospec=True)
    @mock.patch('sm.cleanup._abort')
    def test_lock_not_released_by_abort_when_not_held(
            self,
            mock_abort,
            mock_sr):
        """
        If _abort returns False don't release lockGCActive and ensure that
        False returned by _abort is passed on.
        """
        self.setup_mock_sr(mock_sr)

        # Fake _abort returning False.
        mock_abort.return_value = False

        # Mock lock release function.
        cleanup.lockGCActive = TestRelease()
        cleanup.lockGCActive.release = mock.Mock(return_value=None)

        ret = cleanup.abort(mock_sr, False)

        # Make sure pass on False returned by _abort
        self.assertEqual(False, ret)

        # Make sure we did not release the lock as we don't have it.
        self.assertEqual(cleanup.lockGCActive.release.call_count, 0)

    @mock.patch('sm.cleanup._abort')
    @mock.patch('sm.cleanup.input')
    def test_abort_optional_renable_active_held(
            self,
            mock_input,
            mock_abort):
        """
        Cli has option to re enable gc make sure we release the locks
        correctly if _abort returns True.
        """
        mock_abort.return_value = True
        mock_input.return_value = None

        self.mock_cleanup_locks()

        cleanup.abort_optional_reenable(None)

        # Make sure released lockGCActive
        self.assertEqual(cleanup.lockGCActive.release.call_count, 1)

        # Make sure released lockRunning
        self.assertEqual(cleanup.lockGCRunning.release.call_count, 1)

    @mock.patch('sm.cleanup._abort')
    @mock.patch('sm.cleanup.input')
    def test_abort_optional_renable_active_not_held(
            self,
            mock_input,
            mock_abort):
        """
        Cli has option to reenable gc make sure we release the locks
        correctly if _abort return False.
        """
        mock_abort.return_value = False
        mock_input.return_value = None

        self.mock_cleanup_locks()

        cleanup.abort_optional_reenable(None)

        # Don't release lockGCActive, we don't hold it.
        self.assertEqual(cleanup.lockGCActive.release.call_count, 0)

        # Make sure released lockRunning
        self.assertEqual(cleanup.lockGCRunning.release.call_count, 1)

    @mock.patch('sm.cleanup.init')
    def test__abort_returns_true_when_get_lock(
            self,
            mock_init):
        """
        _abort should return True when it can get
        the lockGCActive straight off the bat.
        """
        cleanup.lockGCActive = AlwaysFreeLock()
        ret = cleanup._abort(None)
        self.assertEqual(ret, True)

    @mock.patch('sm.cleanup.init')
    def test__abort_return_false_if_flag_not_set(
            self,
            mock_init):
        """
        If flag not set return False.
        """
        mock_init.return_value = None

        # Fake the flag returning False.
        self.mock_IPCFlag.return_value.set.return_value = False

        # Not important for this test but we call it so mock it.
        cleanup.lockGCActive = AlwaysLockedLock()

        ret = cleanup._abort(None)

        self.assertEqual(self.mock_IPCFlag.return_value.set.call_count, 1)
        self.assertEqual(ret, False)

    @mock.patch('sm.cleanup.init')
    def test__abort_should_raise_if_cant_get_lock(self, mock_init):
        """
        _abort should raise an exception if it completely
        fails to get lockGCActive.
        """
        mock_init.return_value = None

        # Fake return true so we don't bomb out straight away.
        self.mock_IPCFlag.return_value.set.return_value = True

        # Fake never getting the lock.
        cleanup.lockGCActive = AlwaysLockedLock()

        with self.assertRaises(util.CommandException):
            cleanup._abort(None)

    @mock.patch('sm.cleanup.init')
    def test__abort_should_succeed_if_aquires_on_second_attempt(
            self,
            mock_init
            ):
        """
        _abort should succeed if gets lock on second attempt
        """
        mock_init.return_value = None

        # Fake return true so we don't bomb out straight away.
        self.mock_IPCFlag.return_value.set.return_value = True

        # Use side effect to fake failing to get the lock
        # on the first call, succeeding on the second.
        mocked_lock = AlwaysLockedLock()
        mocked_lock.acquireNoblock = mock.Mock()
        mocked_lock.acquireNoblock.side_effect = [False, True]
        cleanup.lockGCActive = mocked_lock

        ret = cleanup._abort(None)

        self.assertEqual(mocked_lock.acquireNoblock.call_count, 2)
        self.assertEqual(ret, True)

    @mock.patch('sm.cleanup.init')
    def test__abort_should_fail_if_reaches_maximum_retries_for_lock(
            self,
            mock_init
            ):
        """
        _abort should fail if we max out the number of attempts for
        obtaining the lock.
        """
        mock_init.return_value = None

        # Fake return true so we don't bomb out straight away.
        self.mock_IPCFlag.return_value.set.return_value = True

        # Fake a series of failed attempts to get the lock.
        mocked_lock = AlwaysLockedLock()
        mocked_lock.acquireNoblock = mock.Mock()

        # +1 to SR.LOCK_RETRY_ATTEMPTS as we attempt to get lock
        # once outside the loop.
        side_effect = [False] * (cleanup.SR.LOCK_RETRY_ATTEMPTS + 1)

        # Make sure we are not trying once again
        side_effect.append(True)

        mocked_lock.acquireNoblock.side_effect = side_effect
        cleanup.lockGCActive = mocked_lock

        # We've failed repeatedly to gain the lock so raise exception.
        with self.assertRaises(util.CommandException) as te:
            cleanup._abort(None)

        the_exception = te.exception
        self.assertIsNotNone(the_exception)
        self.assertEqual(errno.ETIMEDOUT, the_exception.code)
        self.assertEqual(mocked_lock.acquireNoblock.call_count,
                         cleanup.SR.LOCK_RETRY_ATTEMPTS + 1)

    @mock.patch('sm.cleanup.init')
    def test__abort_succeeds_if_gets_lock_on_final_attempt(self, mock_init):
        """
        _abort succeeds if we get the lockGCActive on the final retry
        """
        mock_init.return_value = None
        self.mock_IPCFlag.return_value.set.return_value = True
        mocked_lock = AlwaysLockedLock()
        mocked_lock.acquireNoblock = mock.Mock()

        # +1 to SR.LOCK_RETRY_ATTEMPTS as we attempt to get lock
        # once outside the loop.
        side_effect = [False] * (cleanup.SR.LOCK_RETRY_ATTEMPTS)

        # On the final attempt we succeed.
        side_effect.append(True)

        mocked_lock.acquireNoblock.side_effect = side_effect
        cleanup.lockGCActive = mocked_lock

        ret = cleanup._abort(None)
        self.assertEqual(mocked_lock.acquireNoblock.call_count,
                         cleanup.SR.LOCK_RETRY_ATTEMPTS + 1)
        self.assertEqual(ret, True)

    @mock.patch('sm.cleanup.lock', autospec=True)
    def test_file_vdi_delete(self, mock_lock):
        """
        Test to confirm fix for HFX-651
        """
        mock_lock.Lock = mock.MagicMock(spec=lock.Lock)

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()

        vdi = cleanup.VDI(sr, str(vdi_uuid), False)

        vdi.delete()
        mock_lock.Lock.cleanupAll.assert_called_with(str(vdi_uuid))

    @mock.patch('sm.cleanup.VDI', autospec=True)
    @mock.patch('sm.cleanup.SR._liveLeafCoalesce', autospec=True)
    @mock.patch('sm.cleanup.SR._snapshotCoalesce', autospec=True)
    def test_coalesceLeaf(self, mock_srSnapshotCoalesce,
                          mock_srLeafCoalesce, mock_vdi):

        mock_vdi.canLiveCoalesce.return_value = True
        mock_srLeafCoalesce.return_value = "This is a test"
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)

        res = sr._coalesceLeaf(vdi)
        self.assertEqual(res, "This is a test")
        self.assertEqual(sr._liveLeafCoalesce.call_count, 1)
        self.assertEqual(sr._snapshotCoalesce.call_count, 0)

    @mock.patch('sm.cleanup.VDI', autospec=True)
    @mock.patch('sm.cleanup.SR._liveLeafCoalesce', autospec=True)
    @mock.patch('sm.cleanup.SR._snapshotCoalesce', autospec=True)
    def test_coalesceLeaf_coalesce_failed(self,
                                          mock_srSnapshotCoalesce,
                                          mock_srLeafCoalesce,
                                          mock_vdi):

        mock_vdi.canLiveCoalesce.return_value = False
        mock_srSnapshotCoalesce.return_value = False
        mock_srLeafCoalesce.return_value = False
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)

        res = sr._coalesceLeaf(vdi)
        self.assertFalse(res)

    @mock.patch('sm.cleanup.VDI.canLiveCoalesce', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.getSizeVHD', autospec=True)
    @mock.patch('sm.cleanup.SR._snapshotCoalesce', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.Util.log')
    def test_coalesceLeaf_size_bigger(self, mock_log,
                                      mock_snapshotCoalesce, mock_vhdSize,
                                      mock_vdiLiveCoalesce):

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)

        mock_vhdSize.side_effect = iter([1024, 4096, 4096, 8000, 8000, 16000])

        sr._snapshotCoalesce = mock.MagicMock(autospec=True)
        sr._snapshotCoalesce.return_value = True

        with self.assertRaises(util.SMException) as exc:
            sr._coalesceLeaf(vdi)

        self.assertIn("VDI {uuid} could not be"
                      " coalesced".format(uuid=vdi_uuid),
                      str(exc.exception))

    @mock.patch('sm.cleanup.VDI.canLiveCoalesce', autospec=True)
    @mock.patch('sm.cleanup.VDI.getSizeVHD', autospec=True)
    @mock.patch('sm.cleanup.SR._snapshotCoalesce', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.SR._liveLeafCoalesce', autospec=True,
                return_value="This is a Test")
    @mock.patch('sm.cleanup.Util.log')
    def test_coalesceLeaf_success_after_4_iterations(self,
                                                     mock_log,
                                                     mock_liveLeafCoalesce,
                                                     mock_snapshotCoalesce,
                                                     mock_vhdSize,
                                                     mock_vdiLiveCoalesce):
        mock_vdiLiveCoalesce.side_effect = iter([False, False, False, True])
        mock_snapshotCoalesce.side_effect = iter([True, True, True])
        mock_vhdSize.side_effect = iter([1024, 1023, 1023, 1022, 1022, 1021])

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)

        res = sr._coalesceLeaf(vdi)

        self.assertEqual(res, "This is a Test")
        self.assertEqual(4, mock_vdiLiveCoalesce.call_count)
        self.assertEqual(3, mock_snapshotCoalesce.call_count)
        self.assertEqual(6, mock_vhdSize.call_count)

    @mock.patch('sm.cleanup.Util.log')
    def test_findLeafCoalesceable_forbidden1(self, mock_log):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.xapi.srRecord = {"other_config": {cleanup.VDI.DB_COALESCE: "false"}}

        res = sr.findLeafCoalesceable()
        self.assertEqual(res, [])
        mock_log.assert_called_with("Coalesce disabled for this SR")

    @mock.patch('sm.cleanup.Util.log')
    def test_findLeafCoalesceable_forbidden2(self, mock_log):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.xapi.srRecord = \
            {"other_config":
             {cleanup.VDI.DB_LEAFCLSC: cleanup.VDI.LEAFCLSC_DISABLED}}

        res = sr.findLeafCoalesceable()
        self.assertEqual(res, [])
        mock_log.assert_called_with("Leaf-coalesce disabled for this SR")

    @mock.patch('sm.cleanup.Util.log')
    def test_findLeafCoalesceable_forbidden3(self, mock_log):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.xapi.srRecord = {"other_config":
                            {cleanup.VDI.DB_LEAFCLSC:
                             cleanup.VDI.LEAFCLSC_DISABLED,
                             cleanup.VDI.DB_COALESCE:
                             "false"}}

        res = sr.findLeafCoalesceable()
        self.assertEqual(res, [])
        mock_log.assert_called_with("Coalesce disabled for this SR")

    @mock.patch('sm.cleanup.Util.log')
    def test_findLeafCoalesceable_forbidden4(self, mock_log):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.xapi.srRecord = {"other_config": {cleanup.VDI.DB_LEAFCLSC:
                                             cleanup.VDI.LEAFCLSC_DISABLED,
                                             cleanup.VDI.DB_COALESCE:
                                             "true"}}

        res = sr.findLeafCoalesceable()
        self.assertEqual(res, [])
        mock_log.assert_called_with("Leaf-coalesce disabled for this SR")

    @mock.patch('sm.cleanup.Util.log')
    def test_findLeafCoalesceable_forbidden5(self, mock_log):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.xapi.srRecord = {"other_config": {cleanup.VDI.DB_LEAFCLSC:
                                             cleanup.VDI.LEAFCLSC_FORCE,
                                             cleanup.VDI.DB_COALESCE:
                                             "false"}}

        res = sr.findLeafCoalesceable()
        self.assertEqual(res, [])
        mock_log.assert_called_with("Coalesce disabled for this SR")
# Utils for testing gatherLeafCoalesceable.

    def srWithOneGoodVDI(self, mock_getConfig, goodConfig):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))

        vdi_uuid = uuid4()
        if goodConfig:
            mock_getConfig.side_effect = goodConfig
        else:
            mock_getConfig.side_effect = iter(["good", False, "blah", "blah"])
        good = cleanup.VDI(sr, str(vdi_uuid), False)
        sr.vdis = {"good": good}
        return sr, good

    def addBadVDITOSR(self, sr, config, coalesceable=True):
        vdi_uuid = uuid4()
        bad = cleanup.VDI(sr, str(vdi_uuid), False)
        bad.getConfig = mock.MagicMock(side_effect=iter(config))
        bad.isLeafCoalesceable = mock.MagicMock(return_value=coalesceable)
        sr.vdis.update({"bad": bad})
        return bad

    def gather_candidates(self, mock_getConfig, config, coalesceable=True,
                          failed=False, expected=None, goodConfig=None):
        sr, good = self.srWithOneGoodVDI(mock_getConfig, goodConfig)
        bad = self.addBadVDITOSR(sr, config, coalesceable=coalesceable)
        if failed:
            sr._failedCoalesceTargets = [bad]

        res = []
        sr.gatherLeafCoalesceable(res)
        self.assertEqual(res, [good])

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", True)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_leaf_not_coalescable(self, mock_getConfig,
                                                    mock_isLeafCoalesceable,
                                                    mock_leafCoalesceForbidden
                                                    ):

        """ The bad vdi returns false for isLeafCoalesceable and is not
            added to the list.
        """
        self.gather_candidates(mock_getConfig,
                               iter(["blah", False, "blah", "blah"]),
                               coalesceable=False)

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", True)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_failed_candidates(self,
                                                 mock_getConfig,
                                                 mock_isLeafCoalesceable,
                                                 mock_leafCoalesceForbidden):

        """ The bad vdi is in the failed list so is not added to the list."""
        self.gather_candidates(mock_getConfig, iter(["blah", False, "blah",
                                                     "blah"]), failed=True)

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", True)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_reset(self, mock_getConfig,
                                     mock_isLeafCoalesceable,
                                     mock_leafCoalesceForbidden):

        """bad has cleanup.VDI.ONBOOT_RESET so not added to list"""
        self.gather_candidates(mock_getConfig,
                               iter([cleanup.VDI.ONBOOT_RESET, False, "blah",
                                     "blah"]))

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", True)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_caching_allowed(self, mock_getConfig,
                                               mock_isLeafCoalesceable,
                                               mock_leafCoalesceForbidden):

        """Bad candidate has caching allowed so not added"""
        self.gather_candidates(mock_getConfig, iter(["blah", True, "blah",
                                                     "blah"]))

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", True)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_clsc_disabled(self, mock_getConfig,
                                             mock_isLeafCoalesceable,
                                             mock_leafCoalesceForbidden):
        """clsc disabled so not added"""
        self.gather_candidates(mock_getConfig,
                               iter(["blah", False,
                                     cleanup.VDI.LEAFCLSC_DISABLED,
                                     "blah"]))

    @mock.patch("sm.cleanup.AUTO_ONLINE_LEAF_COALESCE_ENABLED", False)
    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.VDI.isLeafCoalesceable', autospec=True,
                return_value=True)
    @mock.patch('sm.cleanup.VDI.getConfig', autospec=True)
    def test_gather_candidates_auto_coalesce_off(self, mock_getConfig,
                                                 mock_isLeafCoalesceable,
                                                 mock_leafCoalesceForbidden):
        """Globally turned off but good vdi has force"""
        self.gather_candidates(mock_getConfig,
                               iter(["blah", False, "blah", "blah"]),
                               goodConfig=iter(["blah", False, "blah",
                                                cleanup.VDI.LEAFCLSC_FORCE]))

    def makeVDIReturningSize(self, sr, size, canLiveCoalesce, liveSize):
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)
        vdi._calcExtraSpaceForSnapshotCoalescing = \
            mock.MagicMock(return_value=size)
        vdi.canLiveCoalesce = mock.MagicMock(return_value=canLiveCoalesce)
        vdi._calcExtraSpaceForLeafCoalescing = \
            mock.MagicMock(return_value=liveSize)
        vdi.setConfig = mock.MagicMock()
        return vdi

    def findLeafCoalesceable(self, mock_gatherLeafCoalesceable, goodSize,
                             canLiveCoalesce=False, liveSize=None,
                             expectedNothing=False):

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))

        good = self.makeVDIReturningSize(sr, goodSize, canLiveCoalesce,
                                         liveSize)
        bad = self.makeVDIReturningSize(sr, 4096, False, 4096)

        def fakeCandidates(blah, stuff):
            stuff.append(good)
            stuff.append(bad)

        mock_gatherLeafCoalesceable.side_effect = fakeCandidates
        res = sr.findLeafCoalesceable()
        if expectedNothing:
            self.assertEqual(res, None)
        else:
            self.assertEqual(res, good)
        return good, bad

    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.SR.getFreeSpace', autospec=True, return_value=1024)
    @mock.patch('sm.cleanup.SR.gatherLeafCoalesceable', autospec=True)
    def test_insufficient_space(self, mock_gatherLeafCoalesceable,
                                mock_getFreeSpace,
                                mock_leafCoalesceForbidden):
        """Good vdi calculates space less than remaining on sr"""
        self.findLeafCoalesceable(mock_gatherLeafCoalesceable, 4)

    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.SR.getFreeSpace', autospec=True, return_value=1024)
    @mock.patch('sm.cleanup.SR.gatherLeafCoalesceable', autospec=True)
    def test_space_equal(self, mock_gatherLeafCoalesceable,
                         mock_getFreeSpace,
                         mock_leafCoalesceForbidden):
        """Good has calculates space equal to remaining space"""
        self.findLeafCoalesceable(mock_gatherLeafCoalesceable, 1024)

    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.SR.getFreeSpace', autospec=True, return_value=1024)
    @mock.patch('sm.cleanup.SR.gatherLeafCoalesceable', autospec=True)
    def test_fall_back_to_leaf_coalescing(self, mock_gatherLeafCoalesceable,
                                          mock_getFreeSpace,
                                          mock_leafCoalesceForbidden):
        """Good VDI can can live coalesce and has right size"""
        self.findLeafCoalesceable(mock_gatherLeafCoalesceable, 4096,
                                  canLiveCoalesce=True,
                                  liveSize=4)

    @mock.patch('sm.cleanup.SR.leafCoalesceForbidden', autospec=True,
                return_value=False)
    @mock.patch('sm.cleanup.SR.getFreeSpace', autospec=True, return_value=1024)
    @mock.patch('sm.cleanup.SR.gatherLeafCoalesceable', autospec=True)
    def test_leaf_coalescing_cannt_live_coalesce(self,
                                                 mock_gatherLeafCoalesceable,
                                                 mock_getFreeSpace,
                                                 mock_leafCoalesceForbidden):
        """1st VDI is too big for snap but right size for live
        2nd VDI is too big for snap and too big for live"""
        vdi1, vdi2 = self.findLeafCoalesceable(mock_gatherLeafCoalesceable,
                                               4097,
                                               canLiveCoalesce=False,
                                               liveSize=4,
                                               expectedNothing=True)
        vdi1.setConfig.assert_called_with(cleanup.VDI.DB_LEAFCLSC,
                                          cleanup.VDI.LEAFCLSC_OFFLINE)
        self.assertEqual(vdi2.setConfig.call_count, 0)

    def test_calcStorageSpeed(self):
        sr_uuid = uuid4()
        xapi = mock.MagicMock(autospec=True)
        sr = cleanup.SR(uuid=sr_uuid, xapi=xapi, createLock=False, force=False)
        self.assertEqual(sr.calcStorageSpeed(0, 2, 5), 2.5)
        self.assertEqual(sr.calcStorageSpeed(0.0, 2.0, 5.0), 2.5)
        self.assertEqual(sr.calcStorageSpeed(0.0, 0.0, 5.0), None)

    def test_recordStorageSpeed_bad_speed(self):
        sr_uuid = uuid4()
        xapi = mock.MagicMock(autospec=True)
        sr = cleanup.SR(uuid=sr_uuid, xapi=xapi, createLock=False, force=False)
        sr.writeSpeedToFile = mock.MagicMock(autospec=True)
        sr.recordStorageSpeed(0, 0, 0)
        self.assertEqual(sr.writeSpeedToFile.call_count, 0)

    def test_recordStorageSpeed_good_speed(self):
        sr_uuid = uuid4()
        xapi = mock.MagicMock(autospec=True)
        sr = cleanup.SR(uuid=sr_uuid, xapi=xapi, createLock=False, force=False)
        sr.writeSpeedToFile = mock.MagicMock(autospec=True)
        sr.recordStorageSpeed(1, 6, 9)
        self.assertEqual(sr.writeSpeedToFile.call_count, 1)
        sr.writeSpeedToFile.assert_called_with(1.8)

    def makeFakeFile(self):
        FakeFile.writelines = mock.MagicMock()
        FakeFile.write = mock.MagicMock()
        FakeFile.readlines = mock.MagicMock()
        FakeFile.close = mock.MagicMock()
        FakeFile.seek = mock.MagicMock()
        return FakeFile

    def getStorageSpeed(self, mock_lock, mock_unlock, mock_isFile, sr,
                        fakeFile, isFile, expectedRes, closeCount,
                        lines=None):
        fakeFile.close.call_count = 0
        mock_lock.reset_mock()
        mock_unlock.reset_mock()
        mock_isFile.return_value = isFile
        if lines:
            FakeFile.readlines.return_value = lines
        res = sr.getStorageSpeed()
        self.assertEqual(res, expectedRes)

        self.assertEqual(fakeFile.close.call_count, closeCount)
        self.assertEqual(mock_lock.call_count, 1)
        self.assertEqual(mock_unlock.call_count, 1)

    @mock.patch("builtins.open", autospec=True)
    @mock.patch("os.path.isfile", autospec=True)
    @mock.patch("os.chmod", autospec=True)
    @mock.patch("sm.cleanup.SR.lock", autospec=True)
    @mock.patch("sm.cleanup.SR.unlock", autospec=True)
    def test_getStorageSpeed(self, mock_unlock, mock_lock, mock_chmod,
                             mock_isFile, mock_open):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        fakeFile = self.makeFakeFile()
        mock_open.return_value = FakeFile

        # File does not exist
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             False, None, 0)

        # File exists but empty (should be impossible)
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             True, None, 1, lines=[])

        # File exists one value
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             True, 2.0, 1, lines=["2.0"])

        # File exists 3 values
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             True, 3.0, 1, lines=["1.0", "2.0", "6.0"])

        # File exists contains, a string
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             True, None, 1, lines=["1.0", "2.0", "Hello"])

        # File exists contains, a string
        self.getStorageSpeed(mock_lock, mock_unlock, mock_isFile, sr, fakeFile,
                             True, None, 1, lines=[1.0, 2.0, "Hello"])

    def speedFileSetup(self, sr, FakeFile, mock_isFile, isFile):
        expectedPath = cleanup.SPEED_LOG_ROOT.format(uuid=sr.uuid)
        mock_isFile.return_value = isFile
        FakeFile.writelines.reset_mock()
        FakeFile.write.reset_mock()
        FakeFile.readlines.reset_mock()
        FakeFile.close.reset_mock()
        FakeFile.seek.reset_mock()
        return expectedPath

    def writeSpeedFile(self, mock_lock, mock_unlock, sr, speed, mock_isFile,
                       isFile, mock_open, mock_atomicWrite, write=None,
                       readLines=None, openOp="r+"):
        mock_open.reset_mock()
        mock_atomicWrite.reset_mock()
        mock_lock.reset_mock()
        mock_unlock.reset_mock()
        expectedPath = self.speedFileSetup(sr, FakeFile, mock_isFile, isFile)
        FakeFile.readlines.return_value = readLines
        sr.writeSpeedToFile(speed)

        if isFile:
            mock_open.assert_called_with(expectedPath, openOp)
            self.assertEqual(FakeFile.close.call_count, 1)

        mock_atomicWrite.assert_called_with(expectedPath, cleanup.VAR_RUN,
                                            write)

        self.assertEqual(mock_lock.call_count, 1)
        self.assertEqual(mock_unlock.call_count, 1)

    @mock.patch("builtins.open",
                autospec=True)
    @mock.patch("sm.cleanup.os.path.isfile", autospec=True)
    @mock.patch("sm.cleanup.util.atomicFileWrite", autospec=True)
    @mock.patch("sm.cleanup.SR.lock", autospec=True)
    @mock.patch("sm.cleanup.SR.unlock", autospec=True)
    def test_writeSpeedToFile(self, mock_lock, mock_unlock, mock_atomicWrite,
                              mock_isFile, mock_open):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        FakeFile = self.makeFakeFile()
        mock_open.return_value = FakeFile

        # File does not exist
        self.writeSpeedFile(mock_lock, mock_unlock, sr, 1.8, mock_isFile,
                            False, mock_open, mock_atomicWrite,
                            write="1.8\n", openOp="w")

        # File does exist but empty (Should not happen)
        readLines = []
        write = "1.8\n"
        self.writeSpeedFile(mock_lock, mock_unlock, sr, 1.8, mock_isFile, True,
                            mock_open, mock_atomicWrite, readLines=readLines,
                            write=write)

        # File does exist, exception fired, make sure close fd.
        mock_lock.reset_mock()
        mock_unlock.reset_mock()
        expectedPath = self.speedFileSetup(sr, FakeFile, mock_isFile, True)
        FakeFile.readlines.side_effect = Exception
        with self.assertRaises(Exception):
            sr.writeSpeedToFile(1.8)
        mock_open.assert_called_with(expectedPath, 'r+')
        self.assertEqual(FakeFile.close.call_count, 1)
        self.assertEqual(mock_lock.call_count, 1)
        self.assertEqual(mock_unlock.call_count, 1)
        FakeFile.readlines.side_effect = None

        # File does exist
        readLines = ["1.9\n", "2.1\n", "3\n"]
        write = "1.9\n2.1\n3\n1.8\n"
        self.writeSpeedFile(mock_lock, mock_unlock, sr, 1.8, mock_isFile, True,
                            mock_open, mock_atomicWrite, readLines=readLines,
                            write=write)

        # File does exist and almost full
        readLines = ["2.0\n",
                     "2.1\n",
                     "2.2\n",
                     "2.3\n",
                     "2.4\n",
                     "2.5\n",
                     "2.6\n",
                     "2.7\n",
                     "2.8\n"]

        write = "2.0\n2.1\n2.2\n2.3\n2.4\n2.5\n2.6\n2.7\n2.8\n1.8\n"

        self.writeSpeedFile(mock_lock, mock_unlock, sr, 1.8, mock_isFile, True,
                            mock_open, mock_atomicWrite, readLines=readLines,
                            write=write)

        # File does exist and full
        readLines = ["2.0\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n",
                     "1.9\n"]

        write = "1.9\n1.9\n1.9\n1.9\n1.9\n1.9\n1.9\n1.9\n1.9\n1.9\n1.8\n"

        self.writeSpeedFile(mock_lock, mock_unlock, sr, 1.8, mock_isFile, True,
                            mock_open, mock_atomicWrite, readLines=readLines,
                            write=write)

    def canLiveCoalesce(self, vdi, size, config, speed, expectedRes):
        vdi.getAllocatedSize = mock.MagicMock(return_value=size)
        vdi.getConfig = mock.MagicMock(return_value=config)
        res = vdi.canLiveCoalesce(speed)
        self.assertEqual(res, expectedRes)

    def test_canLiveCoalesce(self):
        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        vdi_uuid = uuid4()
        vdi = cleanup.VDI(sr, str(vdi_uuid), False)
        # Fast enough to for size 10/10 = 1 second and not forcing
        self.canLiveCoalesce(vdi, 10, "blah", 10, True)

        # To slow 10/0.1 = 100 seconds and not forcing
        self.canLiveCoalesce(vdi, 10, "blah", 0.1, False)

        # Fast enough to for size 10/10 = 1 second and forcing
        self.canLiveCoalesce(vdi, 10, cleanup.VDI.LEAFCLSC_FORCE, 10, True)

        # To slow 10/0.1 = 100 seconds and forcing
        self.canLiveCoalesce(vdi, 10, cleanup.VDI.LEAFCLSC_FORCE, 0.1, True)

        # Fallback to hardcoded data size, too big
        self.canLiveCoalesce(vdi, cleanup.VDI.LIVE_LEAF_COALESCE_MAX_SIZE + 1,
                             "blah", None, False)

        # Fallback to hardcoded data size, too big but force
        self.canLiveCoalesce(vdi, cleanup.VDI.LIVE_LEAF_COALESCE_MAX_SIZE + 1,
                             cleanup.VDI.LEAFCLSC_FORCE, None, True)

        # Fallback to hardcoded data size, acceptable size.
        self.canLiveCoalesce(vdi, 10, "blah", None, True)

        # Fallback to hardcoded data size, acceptable size, force also.
        self.canLiveCoalesce(vdi, 10, cleanup.VDI.LEAFCLSC_FORCE, None, True)

    def test_getSwitch(self):
        sr_uuid = uuid4()
        xapi = mock.MagicMock(autospec=True)
        sr = cleanup.SR(uuid=sr_uuid, xapi=xapi, createLock=False, force=False)
        xapi.srRecord = {"other_config": {"test1": "test1", "test2": "test2"}}
        self.assertEqual(sr.getSwitch("test1"), "test1")
        self.assertEqual(sr.getSwitch("test2"), "test2")
        self.assertEqual(sr.getSwitch("test3"), None)

    def forbiddenBySwitch(self, sr, mock_log, switch, switchValue, failMessage,
                          expectedRes):
        mock_log.reset_mock()
        res = sr.forbiddenBySwitch(switch, switchValue, failMessage)
        self.assertEqual(res, expectedRes)
        sr.getSwitch.assert_called_with(switch)
        if failMessage:
            mock_log.assert_called_with(failMessage)
        else:
            self.assertEqual(mock_log.call_count, 0)

    @mock.patch('sm.cleanup.Util.log')
    @mock.patch('sm.cleanup.SR.getSwitch')
    def test_forbiddenBySwitch(self, mock_getSwitch, mock_log):
        sr_uuid = uuid4()
        switch = "blah"
        switchValue = "test"
        failMessage = "This is a test"

        mock_getSwitch.return_value = switchValue
        xapi = mock.MagicMock(autospec=True)
        sr = cleanup.SR(uuid=sr_uuid, xapi=xapi, createLock=False, force=False)

        self.forbiddenBySwitch(sr, mock_log, switch, switchValue, failMessage,
                               True)

        self.forbiddenBySwitch(sr, mock_log, switch, "notForbidden", None,
                               False)

        mock_getSwitch.return_value = None
        self.forbiddenBySwitch(sr, mock_log, switch, "notForbidden", None,
                               False)

    def leafCoalesceForbidden(self, sr, mock_srforbiddenBySwitch, side_effect,
                              expectedRes, expected_callCount, *argv):
        mock_srforbiddenBySwitch.call_count = 0
        mock_srforbiddenBySwitch.side_effect = side_effect
        res = sr.leafCoalesceForbidden()
        self.assertEqual(res, expectedRes)
        sr.forbiddenBySwitch.assert_called_with(*argv)
        self.assertEqual(expected_callCount, sr.forbiddenBySwitch.call_count)

    @mock.patch('sm.cleanup.SR.forbiddenBySwitch', autospec=True)
    def test_leafCoalesceForbidden(self, mock_srforbiddenBySwitch):
        sr = create_cleanup_sr(self.xapi_mock)

        side_effect = iter([True, True])
        self.leafCoalesceForbidden(sr, mock_srforbiddenBySwitch, side_effect,
                                   True, 1, sr, cleanup.VDI.DB_COALESCE,
                                   "false",
                                   "Coalesce disabled "
                                   "for this SR")
        side_effect = iter([True, False])
        self.leafCoalesceForbidden(sr, mock_srforbiddenBySwitch, side_effect,
                                   True, 1, sr, cleanup.VDI.DB_COALESCE,
                                   "false",
                                   "Coalesce disabled "
                                   "for this SR")

        side_effect = iter([False, False])
        self.leafCoalesceForbidden(sr, mock_srforbiddenBySwitch,
                                   side_effect, False, 2, sr,
                                   cleanup.VDI.DB_LEAFCLSC,
                                   cleanup.VDI.LEAFCLSC_DISABLED,
                                   "Leaf-coalesce disabled"
                                   " for this SR")

        side_effect = iter([False, True])
        self.leafCoalesceForbidden(sr, mock_srforbiddenBySwitch, side_effect,
                                   True, 2, sr,
                                   cleanup.VDI.DB_LEAFCLSC,
                                   cleanup.VDI.LEAFCLSC_DISABLED,
                                   "Leaf-coalesce disabled"
                                   " for this SR")

    def trackerReportOk(self, tracker, expectedHistory, expectedReason,
                        start, finish, minimum):
        _before = cleanup.Util
        cleanup.Util = FakeUtil
        tracker.printReasoning()
        pos = 0
        self.assertEqual(FakeUtil.record[0], "Aborted coalesce")
        pos += 1

        for hist in expectedHistory:
            self.assertEqual(FakeUtil.record[pos], hist)
            pos += 1

        self.assertEqual(FakeUtil.record[pos], expectedReason)
        pos += 1
        self.assertEqual(FakeUtil.record[pos],
                         "Starting size was"
                         "         {size}".format(size=start))
        pos += 1
        self.assertEqual(FakeUtil.record[pos],
                         "Final size was"
                         "            {size}".format(size=finish))
        pos += 1
        self.assertEqual(FakeUtil.record[pos],
                         "Minimum size achieved"
                         " was {size}".format(size=minimum))
        FakeUtil.record = []
        cleanup.Util = _before

    def autopsyTracker(self, tracker, finalRes, expectedHistory,
                       expectedReason, start, finish, minimum):

        self.assertTrue(finalRes)
        self.assertEqual(expectedHistory, tracker.history)
        self.assertEqual(expectedReason, tracker.reason)
        self.trackerReportOk(tracker, expectedHistory,
                             expectedReason, start, finish, minimum)

    def exerciseTracker(self, tracker, sizes1, sizes2, its, expectedHistory,
                        expectedReason, start, finish, minimum):
        for x in range(its):
            size1 = sizes1.pop(0)
            size2 = sizes2.pop(0)
            res = tracker.abortCoalesce(size1, size2)
            self.assertFalse(res)

        size1 = sizes1.pop(0)
        size2 = sizes2.pop(0)
        res = tracker.abortCoalesce(size1, size2)
        self.autopsyTracker(tracker, res, expectedHistory, expectedReason,
                            start, finish, minimum)

    def test_leafCoalesceTracker(self):
        sr = create_cleanup_sr(self.xapi_mock)

        # Test initialization
        tracker = cleanup.SR.CoalesceTracker(sr)
        self.assertEqual(tracker.itsNoProgress, 0)
        self.assertEqual(tracker.its, 0)
        self.assertEqual(tracker.minSize, float("inf"))
        self.assertEqual(tracker.history, [])
        self.assertEqual(tracker.reason, "")
        self.assertEqual(tracker.startSize, None)
        self.assertEqual(tracker.finishSize, None)

        # Increase beyond maximum allowed growth
        expectedHistory = [
            "Iteration: 1 -- Initial size 100 --> Final size 100",
            "Iteration: 2 -- Initial size 100 --> Final size 121",
            "Iteration: 3 -- Initial size 100 --> Final size 141",
        ]
        expectedReason = "Unexpected bump in size," \
                         " compared to minimum achieved"
        res = tracker.abortCoalesce(100, 100)
        self.assertFalse(res)
        res = tracker.abortCoalesce(100, 121)
        self.assertFalse(res)
        res = tracker.abortCoalesce(100, 141)
        self.autopsyTracker(tracker, res, expectedHistory,
                            expectedReason, 100, 141, 100)

    def test_leafCoaleesceTracker_too_many_iterations(self):
        sr = create_cleanup_sr(self.xapi_mock)

        # Test initialization
        tracker = cleanup.SR.CoalesceTracker(sr)

        # 10 iterations no progress 11th fails.
        expectedHistory = [
            "Iteration: 1 -- Initial size 20 --> Final size 19",
            "Iteration: 2 -- Initial size 19 --> Final size 18",
            "Iteration: 3 -- Initial size 18 --> Final size 17",
            "Iteration: 4 -- Initial size 17 --> Final size 16",
            "Iteration: 5 -- Initial size 16 --> Final size 15",
            "Iteration: 6 -- Initial size 15 --> Final size 14",
            "Iteration: 7 -- Initial size 14 --> Final size 13",
            "Iteration: 8 -- Initial size 13 --> Final size 12",
            "Iteration: 9 -- Initial size 12 --> Final size 11",
            "Iteration: 10 -- Initial size 11 --> Final size 10",
            "Iteration: 11 -- Initial size 10 --> Final size 12"
        ]
        expectedReason = "Max iterations (10) exceeded"
        self.exerciseTracker(tracker,
                             [20,19,18,17,16,15,14,13,12,11,10],
                             [19,18,17,16,15,14,13,12,11,10,12],
                             10, expectedHistory,
                             expectedReason, 20, 12, 10)

    def test_leafCoalesceTracker_getting_bigger(self):
        sr = create_cleanup_sr(self.xapi_mock)

        # Test initialization
        tracker = cleanup.SR.CoalesceTracker(sr)

        # 3 iterations getting bigger, then fail
        expectedHistory = [
            "Iteration: 1 -- Initial size 100 --> Final size 101",
            "Iteration: 2 -- Initial size 101 --> Final size 102",
            "Iteration: 3 -- Initial size 102 --> Final size 103",
            "Iteration: 4 -- Initial size 103 --> Final size 104",
            "Iteration: 5 -- Initial size 104 --> Final size 105"
        ]
        expectedReason = "No progress made for 3 iterations"
        self.exerciseTracker(tracker,
                             [100,101,102,103,104],
                             [101,102,103,104,105],
                             4, expectedHistory,
                             expectedReason, 100, 105, 100)

    def runAbortable(self, func, ret, ns, abortTest, pollInterval, timeOut):
        return func()

    def add_vdis_for_coalesce(self, sr):
        vdis = {}

        parent_uuid = str(uuid4())
        parent = cleanup.FileVDI(sr, parent_uuid, False)
        parent.path = '%s.vhd' % (parent_uuid)
        sr.vdis[parent_uuid] = parent
        vdis['parent'] = parent

        vdi_uuid = str(uuid4())
        vdi = cleanup.FileVDI(sr, vdi_uuid, False)
        vdi.path = '%s.vhd' % (vdi_uuid)
        vdi.parent = parent
        # Set an initial value to make Mock happy.
        vdi._sizeAllocated = 20971520 # 10 blocks of 2MB changed in the child.
        parent.children.append(vdi)

        sr.vdis[vdi_uuid] = vdi
        vdis['vdi'] = vdi

        child_vdi_uuid = str(uuid4())
        child_vdi = cleanup.FileVDI(sr, child_vdi_uuid, False)
        child_vdi.path = '%s.vhd' % (child_vdi_uuid)
        vdi.children.append(child_vdi)
        sr.vdis[child_vdi_uuid] = child_vdi
        vdis['child'] = child_vdi

        return vdis

    @mock.patch('sm.cleanup.os.unlink', autospec=True)
    @mock.patch('sm.cleanup.util', autospec=True)
    @mock.patch('sm.cleanup.vhdutil', autospec=True)
    @mock.patch('sm.cleanup.journaler.Journaler', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    def test_coalesce_success(
            self, mock_abortable, mock_journaler, mock_vhdutil, mock_util,
            mock_unlink):
        """
        Non-leaf coalesce
        """
        self.xapi_mock.getConfigVDI.return_value = {}

        mock_abortable.side_effect = self.runAbortable

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.journaler = mock_journaler

        mock_ipc_flag = mock.MagicMock(spec=ipc.IPCFlag)
        self.mock_IPCFlag.return_value = mock_ipc_flag
        mock_ipc_flag.test.return_value = None

        vdis = self.add_vdis_for_coalesce(sr)
        mock_journaler.get.return_value = None

        vdi_uuid = vdis['vdi'].uuid

        sr.coalesce(vdis['vdi'], False)

        mock_journaler.create.assert_has_calls(
            [mock.call('coalesce', vdi_uuid, '1'),
             mock.call('relink', vdi_uuid, '1')])
        mock_journaler.remove.assert_has_calls(
            [mock.call('coalesce', vdi_uuid),
             mock.call('relink', vdi_uuid)])

        self.xapi_mock.getConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'activating')])

        self.xapi_mock.addToConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'relinking', 'True')])

        # Remove twice as set does a remove first and then for completion
        self.xapi_mock.removeFromConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'relinking'),
             mock.call(vdis['child'], 'vhd-parent'),
             mock.call(vdis['child'], 'relinking')])

    @mock.patch('sm.cleanup.os.unlink', autospec=True)
    @mock.patch('sm.cleanup.util', autospec=True)
    @mock.patch('sm.cleanup.vhdutil', autospec=True)
    @mock.patch('sm.cleanup.journaler.Journaler', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    def test_coalesce_error(
            self, mock_abortable, mock_journaler, mock_vhdutil, mock_util,
            mock_unlink):
        """
        Handle errors in coalesce
        """
        mock_util.SMException = util.SMException

        self.xapi_mock.getConfigVDI.return_value = {}

        def run_abortable(func, ret, ns, abortTest, pollInterval, timeOut):
            raise util.SMException("Timed out")

        mock_abortable.side_effect = run_abortable

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.journaler = mock_journaler

        mock_ipc_flag = mock.MagicMock(spec=ipc.IPCFlag)
        self.mock_IPCFlag.return_value = mock_ipc_flag
        mock_ipc_flag.test.return_value = None

        vdis = self.add_vdis_for_coalesce(sr)
        mock_journaler.get.return_value = None

        mock_vhdutil.FILE_EXTN_VHD = vhdutil.FILE_EXTN_VHD
        mock_vhdutil.FILE_EXTN_RAW = vhdutil.FILE_EXTN_RAW
        mock_vhdutil.getParent.return_value = vdis['parent'].path

        sr.coalesce(vdis['vdi'], False)

        self.assertIn(vdis['vdi'], sr._failedCoalesceTargets)
        mock_vhdutil.repair.assert_called_with(vdis['parent'].path)

    @mock.patch('sm.cleanup.os.unlink', autospec=True)
    @mock.patch('sm.cleanup.util', autospec=True)
    @mock.patch('sm.cleanup.vhdutil', autospec=True)
    @mock.patch('sm.cleanup.journaler.Journaler', autospec=True)
    @mock.patch('sm.cleanup.Util.runAbortable')
    def test_coalesce_error_raw_parent(
            self, mock_abortable, mock_journaler, mock_vhdutil, mock_util,
            mock_unlink):
        """
        Handle errors in coalesce with raw parent
        """
        mock_util.SMException = util.SMException

        self.xapi_mock.getConfigVDI.return_value = {}

        def run_abortable(func, ret, ns, abortTest, pollInterval, timeOut):
            raise util.SMException("Timed out")

        mock_abortable.side_effect = run_abortable

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))
        sr.journaler = mock_journaler

        mock_ipc_flag = mock.MagicMock(spec=ipc.IPCFlag)
        self.mock_IPCFlag.return_value = mock_ipc_flag
        mock_ipc_flag.test.return_value = None

        vdis = self.add_vdis_for_coalesce(sr)
        vdis['parent'].raw = True
        mock_journaler.get.return_value = None

        mock_vhdutil.FILE_EXTN_VHD = vhdutil.FILE_EXTN_VHD
        mock_vhdutil.FILE_EXTN_RAW = vhdutil.FILE_EXTN_RAW
        mock_vhdutil.getParent.return_value = vdis['parent'].path

        sr.coalesce(vdis['vdi'], False)

        self.assertIn(vdis['vdi'], sr._failedCoalesceTargets)
        self.assertEqual(0, mock_vhdutil.repair.call_count)

    def test_tag_children_for_relink_activation(self):
        """
        Cleanup: tag for relink, activation races
        """
        self.xapi_mock.getConfigVDI.side_effect = [
            {'activating': 'True'},
            {},
            {}
        ]

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))

        vdis = self.add_vdis_for_coalesce(sr)

        vdis['parent']._tagChildrenForRelink()

        self.xapi_mock.getConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'activating'),
             mock.call(vdis['child'], 'activating'),
             mock.call(vdis['child'], 'activating')])
        self.xapi_mock.addToConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'relinking', 'True')])
        self.assertEqual(1, self.xapi_mock.removeFromConfigVDI.call_count)

    def test_tag_children_for_relink_activation_second_phase(self):
        """
        Cleanup: tag for relink, set and then activation
        """
        self.xapi_mock.getConfigVDI.side_effect = [
            {},
            {'activating': 'True'},
            {},
            {}
        ]

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))

        vdis = self.add_vdis_for_coalesce(sr)

        vdis['parent']._tagChildrenForRelink()

        self.xapi_mock.getConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'activating'),
             mock.call(vdis['child'], 'activating'),
             mock.call(vdis['child'], 'activating'),
             mock.call(vdis['child'], 'activating')])
        self.xapi_mock.addToConfigVDI.assert_has_calls(
            [mock.call(vdis['child'], 'relinking', 'True'),
             mock.call(vdis['child'], 'relinking', 'True')])
        # Remove called 3 times, twice from set, once on failure
        self.assertEqual(3, self.xapi_mock.removeFromConfigVDI.call_count)

    def test_tag_children_for_relink_blocked(self):
        """
        Cleanup: tag for relink, blocked - exception
        """

        self.xapi_mock.getConfigVDI.return_value = {'activating': 'True'}

        sr_uuid = uuid4()
        sr = create_cleanup_sr(self.xapi_mock, uuid=str(sr_uuid))

        vdis = self.add_vdis_for_coalesce(sr)

        with self.assertRaises(util.SMException) as sme:
            vdis['parent']._tagChildrenForRelink()

        self.assertIn('Failed to tag vdi', str(sme.exception))

        self.assertGreater(self.mock_time_sleep.call_count, 5)

    def init_gc_loop_sr(self):
        sr_uuid = str(uuid4())
        mock_sr = mock.MagicMock(spec=cleanup.SR)
        mock_sr.vdis = {}
        mock_sr.xapi = self.xapi_mock
        mock_sr.uuid = sr_uuid
        mock_sr.gcEnabled.return_value = True

        mock_sr.garbageCollect = mock.MagicMock(spec=cleanup.SR.garbageCollect)
        mock_sr.coalesce = mock.MagicMock(spec=cleanup.SR.coalesce)
        mock_sr.coalesceLeaf = mock.MagicMock(spec=cleanup.SR.coalesceLeaf)

        return (sr_uuid, mock_sr)

    @mock.patch('sm.cleanup._create_init_file', autospec=True)
    def test_gcloop_no_work(self, mock_init_file):
        """
        GC exits immediate with no work
        """
        ## Arrange
        sr_uuid, mock_sr = self.init_gc_loop_sr()

        mock_sr.hasWork.return_value = False
        cleanup.lockGCActive.acquireNoblock = mock.Mock(return_value=True)

        ## Act
        cleanup._gcLoop(mock_sr, dryRun=False)

        ## Assert
        mock_init_file.assert_called_with(sr_uuid)

    @mock.patch('sm.cleanup._create_init_file', autospec=True)
    def test_gcloop_no_work2(self, mock_init_file):
        # Given
        sr_uuid, mock_sr = self.init_gc_loop_sr()
        self.xapi_mock.isPluggedHere.return_value = False

        # When
        cleanup._gcLoop(mock_sr, dryRun=False)

        # When
        mock_sr.scanLocked.assert_not_called()


    @mock.patch('sm.cleanup._create_init_file', autospec=True)
    def test_gcloop_one_of_each(self, mock_init_file):
        """
        GC, one garbage, one non-leaf, one leaf
        """
        ## Arrange
        sr_uuid, mock_sr = self.init_gc_loop_sr()
        vdis = self.add_vdis_for_coalesce(mock_sr)

        mock_sr.hasWork.side_effect = [
            True, True, True, False]
        mock_sr.findGarbage.side_effect = [
            [vdis['child']], []]
        mock_sr.findCoalesceable.side_effect = [
            vdis['vdi'], None]
        mock_sr.findLeafCoalesceable.side_effect = [
            vdis['vdi']]

        cleanup.lockGCActive.acquireNoblock = mock.Mock(return_value=True)
        cleanup.lockGCRunning.acquireNoblock = mock.Mock(return_value=True)

        ## Act
        cleanup._gcLoop(mock_sr, dryRun=False)

        ## Assert
        mock_sr.garbageCollect.assert_called_with(False)
        mock_sr.coalesce.assert_called_with(vdis['vdi'], False)
        mock_sr.coalesceLeaf.assert_called_with(vdis['vdi'], False)

    @mock.patch('sm.cleanup.Util')
    @mock.patch('sm.cleanup._gc', autospec=True)
    def test_gc_foreground_is_immediate(self, mock_gc, mock_util):
        """
        GC called in foreground will run immediate
        """
        ## Arrange
        mock_session = mock.MagicMock(name='MockSession')
        sr_uuid = str(uuid4())

        ## Act
        cleanup.gc(mock_session, sr_uuid, inBackground=False)

        ## Assert
        mock_gc.assert_called_with(mock_session, sr_uuid,
                                   False, immediate=True)

    @mock.patch('sm.cleanup.os._exit', autospec=True)
    @mock.patch('sm.cleanup.daemonize', autospec=True)
    @mock.patch('sm.cleanup.Util')
    @mock.patch('sm.cleanup._gc', autospec=True)
    def test_gc_background_is_not_immediate(
            self, mock_gc, mock_util, mock_daemonize, mock_exit):
        """
        GC called in background will daemonize
        """
        ## Arrange
        mock_session = mock.MagicMock(name='MockSession')
        sr_uuid = str(uuid4())
        mock_daemonize.return_value = True

        ## Act
        cleanup.gc(mock_session, sr_uuid, inBackground=True)

        ## Assert
        mock_gc.assert_called_with(None, sr_uuid, False)
        mock_daemonize.assert_called_with()

    def test_not_plugged(self):
        """
        GC called on an SR that is not plugged errors
        """
        # Arrange
        self.xapi_mock.isPluggedHere.return_value = False

        # Act
        with self.assertRaises(util.SMException):
            create_cleanup_sr(self.xapi_mock)

    def test_not_plugged_retry(self):
        """
        GC called on an SR that is not plugged retrys
        """
        # Arrange
        self.xapi_mock.isPluggedHere.side_effect = [
            False, False, False, True]

        # Act
        sr = create_cleanup_sr(self.xapi_mock)

        # Assert
        self.assertIsNotNone(sr)


class TestLockGCActive(unittest.TestCase):

    def setUp(self):
        self.addCleanup(mock.patch.stopall)

        self.lock_patcher = mock.patch('sm.cleanup.lock.Lock')
        patched_lock = self.lock_patcher.start()
        patched_lock.side_effect = self.create_lock
        self.locks = {}

        self.sr_uuid = str(uuid4())

    class DummyLock:
        def __init__(self, name):
            self.name = name
            self.held = False
            self.count = 0
            self.can_acquire = True

        def acquire(self):
            if not self.held:
                if self.can_acquire:
                    self.held = True
                else:
                    # In a real lock this would block, instead, error
                    raise BlockingIOError()

            self.count += 1

        def acquireNoblock(self):
            if self.held or self.can_acquire:
                self.held = True
                return True

            return False

        def release(self):
            self.count -= 1
            if not self.count:
                self.held = False

    def create_lock(self, lock_name, sr_uuid):
        lock_key = f'{lock_name}/{sr_uuid}'
        if lock_key not in self.locks:
            self.locks[lock_key] = self.DummyLock(lock_key)

        return self.locks[lock_key]

    def test_can_acquire(self):
        # Given
        gcLock = cleanup.LockActive(self.sr_uuid)

        # When
        acquired = gcLock.acquireNoblock()

        # Then
        self.assertTrue(acquired)

    def test_can_acquire_when_already_holding_sr_lock(self):
        # Given
        srLock = lock.Lock(vhdutil.LOCK_TYPE_SR, self.sr_uuid)
        srLock.held = True
        gcLock = cleanup.LockActive(self.sr_uuid)

        # When
        count0 = srLock.count

        srLock.acquire()
        count1 = srLock.count

        acquired = gcLock.acquireNoblock()

        if acquired: # pragma: no cover
            gcLock.release()

        srLock.release()
        count2 = srLock.count

        # Then
        self.assertTrue(acquired)
        self.assertEqual(count0, 0)
        self.assertEqual(count1, 1)
        self.assertEqual(count2, 0)

    def test_cannot_acquire_if_other_process_holds_gc_lock(self):
        # Given
        gcLock = cleanup.LockActive(self.sr_uuid)
        gcLock._lock.can_acquire = False

        # When
        acquired = gcLock.acquireNoblock()

        # Then
        self.assertFalse(acquired)

    def test_cannot_acquire_if_other_process_holds_sr_lock(self):
        # Given
        gcLock = cleanup.LockActive(self.sr_uuid)
        gcLock._srLock.can_acquire = False

        # When
        with self.assertRaises(BlockingIOError):
            gcLock.acquireNoblock()
