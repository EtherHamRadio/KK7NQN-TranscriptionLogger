# systemd units

Install to `/etc/systemd/system/`, adjust paths and `User=`, then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now allstar-ptt-recorder allstar-transcribe-watcher allstar-netviewer

The analyzer timer is optional — leave it disabled if you want a transcript
archive with no net detection:

    sudo systemctl enable --now allstar-transcript-analyzer.timer

**Every unit needs `Environment=PYTHONUNBUFFERED=1`.** Without it Python
block-buffers stdout when systemd captures it and journal timestamps become
buffer-flush times rather than event times. Either put it in the unit directly
or add a drop-in:

    sudo systemctl edit allstar-ptt-recorder.service

    [Service]
    Environment=PYTHONUNBUFFERED=1

The user running the recorder must be able to move files out of the staging
directory, which Asterisk owns — add them to the `asterisk` group.
