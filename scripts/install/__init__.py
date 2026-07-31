"""
scripts/install/
=================================
M4.10 §4 Installation Wizard.

    verify.py     Checks Python / dependencies / Docker / Redis / Postgres / NATS.
    doctor.py      Runs verify.py's checks and prints fix suggestions for anything failing.
    install.py     End-to-end: runs verify, offers fixes where possible, then
                  (optionally) runs `alembic upgrade head` and seeds nothing —
                  this platform's own migration/seed tooling is reused, not
                  reimplemented here.

Every check here is read-only / connection-probing; nothing in this
package modifies the target machine except `install.py --apply-fixes`,
which is limited to `pip install -r requirements.txt` and creating
missing local directories — never a package manager outside `pip`,
never a system service start/stop.
"""
