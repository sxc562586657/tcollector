import time
import subprocess
import errno
from collectors.lib.collectorbase import CollectorBase
from Queue import Queue

class Ntp(CollectorBase):

    def __init__(self, config, logger, readq):
        super(Ntp, self).__init__(config, logger, readq)
        self.host = self.get_config('host', "127.0.0.1")
        self.timeout =self.get_config('timeout', '0.5')



    def set_ntp_proc(self):
        try:
            self.ts = time.time()
            self.ntp_proc = subprocess.Popen(["ntpdate", "-q", "-t", str(self.timeout), self.host], stdout=subprocess.PIPE)
        except OSError, e:
            if e.errno == errno.ENOENT:
                self._readq.nput("ntp.state %d %s" % (self.ts, "1"))
                self.log_error("ntpd is error datestamp:%d,message is " % (self.ts, e.message))
            else:
                self._readq.nput("ntp.state %d %s" % (self.ts, "1"))
                self.log_error("ntpd is error datestamp:%d,message is " % (self.ts, e.message))
                raise

    def __call__(self):
        try:
            self.set_ntp_proc()
            success=self.set_offset()
            if success:
                self._readq.nput("ntp.offset %d %s" % (self.ts, self.offset))
                self._readq.nput("ntp.state %d %s" % (self.ts, "0"))
            else:
                self._readq.nput("ntp.state %d %s" % (self.ts, "1"))
                self.log_error("no server suitable for synchronization found")
        except Exception,e:
            self._readq.nput("ntp.state %d %s" % (self.ts, "1"))
            self.log_error("ntpd is error datestamp:%d" % self.ts)

    def set_offset(self):
        stdout, _ = self.ntp_proc.communicate()
        print stdout
        words = stdout.split(',')
        for word in words:
            if 'stratnum' in word:
                if '0' == word.split(',')[1]:
                    return False
            if 'offset' in word:
                self.offset=word.split()[1]
                return True

    def cleanup(self):
        self.log_info('Ntp stop subprocess %d', self.ntp_proc.pid)
        self.stop_subprocess(self.ntp_proc, __name__)

def test():
    ts = time.time()
    ntp=Ntp(None,None,Queue())
    while True:
        ntp.set()
        ntp.set_offset()
        print "ntp.offset %d %s" % (ts, ntp.offset)

if __name__=='__main__':
    test()
       # sleep(10)