"""Windows job lifecycle, including failed termination and supervisor death."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tokenatlas import upgrade_process


class JobCleanupTests(unittest.TestCase):
    def test_failed_termination_waits_for_children_before_raising(self):
        job=upgrade_process._Job.__new__(upgrade_process._Job)
        job.handle=1
        job.api=Mock()
        job.api.TerminateJobObject.return_value=False
        job.active=Mock(side_effect=[1,1,0])
        with patch.object(upgrade_process.ctypes,'WinError',return_value=OSError('termination failed'),create=True), patch.object(upgrade_process.ctypes,'get_last_error',return_value=5,create=True), patch.object(upgrade_process.time,'sleep') as sleep:
            with self.assertRaises(OSError):job.drain()
        self.assertEqual(job.active.call_count,3)
        sleep.assert_called_once()


@unittest.skipUnless(os.name=='nt','native Windows job objects')
class WindowsJobTests(unittest.TestCase):
    def test_assignment_failure_cannot_start_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'started'
            code='from pathlib import Path; Path('+repr(str(marker))+').touch()'
            with patch.object(upgrade_process._Job,'assign',side_effect=OSError('assignment denied')):
                with self.assertRaises(OSError):
                    upgrade_process.execute_windows([sys.executable,'-c',code],dict(os.environ),tmp,10)
            self.assertFalse(marker.exists())

    def test_supervisor_death_kills_installer_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready=Path(tmp)/'ready'
            late=Path(tmp)/'late'
            child='from pathlib import Path; import time; Path('+repr(str(ready))+').touch(); time.sleep(3); Path('+repr(str(late))+').touch()'
            command='import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); time.sleep(30)'
            supervisor=('import os,sys; from tokenatlas.upgrade_process import execute_windows; '
                        'execute_windows([sys.executable,"-c",'+repr(command)+'],dict(os.environ),'+repr(tmp)+',60)')
            proc=subprocess.Popen([sys.executable,'-c',supervisor],cwd=Path(__file__).parent,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                if not ready.exists():
                    proc.kill()
                    self.fail('supervised installer did not start: '+proc.communicate()[1].decode(errors='replace'))
                proc.kill();proc.wait(timeout=10)
                time.sleep(3.5)
                self.assertFalse(late.exists(),'installer survived supervisor death')
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
                if proc.stderr:proc.stderr.close()


if __name__=='__main__':unittest.main()
