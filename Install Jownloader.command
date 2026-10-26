#!/bin/bash
# Double-click installer: runs the same one-command install as the README, in this Terminal window
# (so the password prompt and progress show). Unsigned on purpose: macOS asks once, see README.
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/thuhmadhatter42/jownloader/master/install)"
echo
read -r -p "Press Return to close this window." _
