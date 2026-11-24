REBSYN-LAB Post-Incident Report IR-2026-03-14 (case RB-W01 follow-up)
=====================================================================

1. Summary
At 14:02Z an unregistered host (10.0.0.99) authenticated as ops-admin
on srv-admin01 and read /etc/shadow at 14:03Z. Detection came from a
manual log review at 02:14 the next day, 12 hours after the activity.

2. Timeline (abridged)
14:01Z  badge-in of ops-admin (door LAB-B-ENTRY)
14:02Z  SSH publickey login as ops-admin from 10.0.0.99 (unregistered)
14:03Z  sudo cat /etc/shadow on srv-admin01
14:05Z  outbound connection to update-cache.rebsyn.invalid

3. Contributing factors
3.1 The admin jump-host policy exists but was not enforced: direct
    SSH to srv-admin01 remained possible from the lab segment.
3.2 ops-admin had no MFA on the account (single-factor publickey).
3.3 Badge logs are not centralized, so the badge/auth attribution
    conflict surfaced only during manual review.
3.4 The asset register is stale: 10.0.0.99 was unregistered, and no
    control flagged the login source.
3.5 Shadow-file access by admins is not alerted (no canary on
    /etc/shadow reads).

4. Recommended corrective actions
CA-1 Enforce MFA on all admin accounts (evidence: 3.2)
CA-2 Enforce jump-host-only admin access to servers (evidence: 3.1)
CA-3 Centralize badge access logs into the SIEM (evidence: 3.3)
CA-4 Automate asset-register reconciliation (evidence: 3.4)
CA-5 Canary coverage for sensitive-file reads (evidence: 3.5)
