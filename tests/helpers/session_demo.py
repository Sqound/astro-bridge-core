"""Seed fictional retained nights, then run the unchanged disposable demo CLI."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from wifi_bridge.telemetry import TelemetryStore
from .clock_fixture import event_clock


class FixtureClock:
    mono = 1

    def read(self):
        date = (datetime(2026, 11, 4, 20, tzinfo=timezone.utc)+timedelta(seconds=self.mono)).isoformat()
        return {'system_utc':date,'corrected_utc':None,'source':'system','offset_ms':0,'clock_status':'unverified'}


def seed(folder):
    clock=FixtureClock()
    with_store=TelemetryStore(folder/'telemetry.sqlite3',clock=clock,monotonic=lambda:clock.mono)
    try:
        for gap in (False,True):
            ident=with_store.start_session('vespera-demo','vespera',['vespera_status_v1'])
            with_store.sample(ident,'exposure.seconds',10)
            for number in range(601):
                clock.mono+=10
                for metric,value in {'stack.frames_accepted':number,'stack.frames_rejected':number//10,
                                     'env.temperature_c':6.3,'env.dew_point_c':4.1,'env.humidity_pct':71,
                                     'battery.percent':96-number/30,'target.name':'M 27',
                                     'stack.reject_reason.cloud':number//15,'stack.reject_reason.tracking':number//30}.items():
                    with_store.sample(ident,metric,value)
            with_store.end_session(ident,'idle');with_store.flush()
            rows=with_store.db.execute('SELECT MIN(seq), MAX(seq) FROM samples WHERE session_id=?',(ident,)).fetchone()
            if gap:
                with_store.reader(ident,'Synthetic reader',rows[0],rows[0]+1000)
                with_store.reader(ident,'Synthetic reader',rows[1]-1000,rows[1])
            else:with_store.reader(ident,'Synthetic reader',rows[0],rows[1])
            with_store.flush()
    finally:with_store.close()


if __name__=='__main__':
    if '--demo' not in sys.argv:raise SystemExit('Synthetic sessions require --demo')
    seed(Path(sys.argv[sys.argv.index('--state-dir')+1]))
    from wifi_bridge.__main__ import main
    with event_clock(synchronized=False):main()
