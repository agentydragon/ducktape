# OpenWebRX+ SDR source configuration (classic config layer).
# Connects to the rtl-tcp ClusterIP service for IQ data.
sdrs = {
    "rtlsdr": {
        "name": "RTL-SDR Blog V4",
        "type": "rtl_tcp",
        "remote": "rtl-tcp.sdr.svc.cluster.local:1234",
        "profiles": {
            "fm": {
                "name": "FM Radio",
                "center_freq": 100000000,
                "samp_rate": 2400000,
                "start_freq": 100000000,
                "start_mod": "wfm",
            },
            "airband": {
                "name": "Airband",
                "center_freq": 125000000,
                "samp_rate": 2400000,
                "start_freq": 125000000,
                "start_mod": "am",
            },
            "adsb": {
                "name": "ADS-B (1090 MHz)",
                "center_freq": 1090000000,
                "samp_rate": 2400000,
                "start_freq": 1090000000,
                "start_mod": "am",
            },
        },
    }
}
