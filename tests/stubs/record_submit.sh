#!/usr/bin/env bash
# Stands in for od_qsub.sh and records the argv it was given, so a test can
# check what the submitter ACTUALLY receives rather than what the dry run
# says it would.
printf '%s\n' "$@" > "${OD_SUBMIT_RECORD:?set OD_SUBMIT_RECORD}"
