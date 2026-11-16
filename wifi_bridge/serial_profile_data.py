"""Serial device profiles, as data. A profile owns the serial settings, the read-only
identification frames and the stop sequence sent when a client is lost; the relay only
executes it. Kept as a module (not a data directory) so over-the-air updates can carry it.

Evidence: "documented" = the manufacturer's NexStar hand control serial protocol;
"bench_verified" = seen on a NexStar 4SE (hand control 4.15, motor boards 5.14).
"""

PROFILES = [
  {
    "schema_version": 1,
    "id": "nexstar",
    "name": "Celestron NexStar hand control",
    "protocol": "nexstar",
    "app_preset": "Celestron (NexStar hand control, serial Wi-Fi bridge)",
    "serial": {
      "baud": 9600,
      "format": "8N1",
      "flow_control": "none"
    },
    "default_tcp_port": 2000,
    "usb_hints": [
      {
        "vendor": "067b",
        "product": "2303",
        "label": "Prolific PL2303"
      },
      {
        "vendor": "0403",
        "product": "6001",
        "label": "FTDI FT232"
      }
    ],
    "identify": {
      "parser": "nexstar_v1",
      "reply_terminator": "23",
      "frame_timeout_seconds": 0.6,
      "frames": [
        {
          "id": "echo",
          "hex": "4b78",
          "reply_length": 1
        },
        {
          "id": "version",
          "hex": "56",
          "reply_length": 2
        },
        {
          "id": "model",
          "hex": "6d",
          "reply_length": 1
        },
        {
          "id": "azimuth_board_version",
          "hex": "5001 10fe 0000 0002",
          "reply_length": 2
        },
        {
          "id": "altitude_board_version",
          "hex": "5001 11fe 0000 0002",
          "reply_length": 2
        }
      ]
    },
    "models": {
      "1": "GPS Series",
      "3": "i-Series",
      "4": "i-Series SE",
      "5": "CGE",
      "6": "Advanced GT",
      "7": "SLT",
      "9": "CPC",
      "10": "GT",
      "11": "4/5 SE",
      "12": "6/8 SE"
    },
    "stop_on_loss": {
      "reply_terminator": "23",
      "frame_timeout_seconds": 0.5,
      "frames": [
        {
          "id": "cancel_goto",
          "hex": "4d"
        },
        {
          "id": "azimuth_fixed_positive_zero",
          "hex": "5002 1024 0000 0000"
        },
        {
          "id": "azimuth_fixed_negative_zero",
          "hex": "5002 1025 0000 0000"
        },
        {
          "id": "azimuth_variable_positive_zero",
          "hex": "5003 1006 0000 0000"
        },
        {
          "id": "azimuth_variable_negative_zero",
          "hex": "5003 1007 0000 0000"
        },
        {
          "id": "altitude_fixed_positive_zero",
          "hex": "5002 1124 0000 0000"
        },
        {
          "id": "altitude_fixed_negative_zero",
          "hex": "5002 1125 0000 0000"
        },
        {
          "id": "altitude_variable_positive_zero",
          "hex": "5003 1106 0000 0000"
        },
        {
          "id": "altitude_variable_negative_zero",
          "hex": "5003 1107 0000 0000"
        }
      ]
    },
    "evidence": {
      "documented": [
        "K",
        "V",
        "m",
        "M",
        "P pass-through (fixed and variable rate, get version)"
      ],
      "bench_verified": [
        "K",
        "V",
        "m",
        "M",
        "P get version on both boards",
        "P fixed and variable rate 0, positive side"
      ],
      "not_bench_verified": [
        "P rate 0 on the negative side",
        "reading by the bridge itself"
      ]
    }
  }
]
