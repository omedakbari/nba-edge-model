-- injury_flags: why a flag was closed. NULL = the normal path (the sources agreed again, see
-- jobs/injuries.write_flags). Set by hand when flags are closed for another reason, e.g. the
-- 00:00-06:00 ET rollover false positives closed after fix 71a934a.
alter table injury_flags add column if not exists resolved_reason text;
