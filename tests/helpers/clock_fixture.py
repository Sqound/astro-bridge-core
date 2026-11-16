"""Explicit clock inputs for disposable fixtures, including the real demo CLI."""
from functools import partial
import subprocess
import time
from unittest.mock import patch

from wifi_bridge.clock import Clock


def event_clock(*, synchronized=False):
    """Keep the real Clock implementation; inject only its platform sync probe."""
    return patch('wifi_bridge.events.Clock', partial(Clock, sync=lambda: synchronized))


def chrony_runner(run, *, synchronized=False):
    """Supply synthetic chrony evidence while forwarding other commands unchanged."""
    def execute(args, **kwargs):
        if args[0] != '/usr/bin/chronyc':
            return run(args, **kwargs)
        if not synchronized:
            raise FileNotFoundError('Synthetic chrony unavailable')
        outputs = {
            'tracking': f'C0000201,192.0.2.1,2,{time.time()-1},0.001,0,0,0,0,0,0.01,0.01,64,Normal',
            'sources': '^,*,192.0.2.1,2,6,377,1,0.001,0.001,0.01',
        }
        assert args[1:] in (['-c', '-n', 'tracking'], ['-c', '-n', 'sources'])
        output = outputs[args[-1]] + '\n'
        return subprocess.CompletedProcess(args, 0, output if kwargs.get('text') else output.encode())
    return execute


if __name__ == '__main__':
    from wifi_bridge.__main__ import main
    # Tests still exercise the production startup, workers, HTTP and shutdown.
    with event_clock(synchronized=False):
        main()
