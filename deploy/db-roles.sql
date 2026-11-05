-- The three database roles, and what each is allowed to do. Run as a superuser, in the app's
-- database, with psql. Safe to run again at any time, which is also how passwords are changed.
--
--   selltrack_migrator  owns the schema. Only used to run migrations.
--   selltrack_app       what the app connects as. Reads and writes data, and can't change the
--                       schema. It can only add to the audit log, so a bug or an attacker in the
--                       app can't rewrite the record of what happened.
--   selltrack_backup    reads everything and changes nothing.
--
-- The deploy runs this before and after migrations. Before, so the roles exist. After, so that
-- anything a migration created has the rules below applied.

\set ON_ERROR_STOP on

-- From the environment rather than the command line, where they'd show up in a process list. A
-- missing one leaves its variable unset, which is a syntax error below, so nothing is half done.
\getenv migrator_password DB_MIGRATOR_PASSWORD
\getenv app_password DB_APP_PASSWORD
\getenv backup_password DB_BACKUP_PASSWORD

SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE', role_name)
FROM (VALUES ('selltrack_migrator'), ('selltrack_app'), ('selltrack_backup')) AS wanted (role_name)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name)
\gexec

ALTER ROLE selltrack_migrator PASSWORD :'migrator_password';
ALTER ROLE selltrack_app PASSWORD :'app_password';
ALTER ROLE selltrack_backup PASSWORD :'backup_password';

-- The database's owner owns the public schema too, and can install the extensions the first
-- migration needs (citext and pg_trgm are both trusted extensions).
SELECT format('ALTER DATABASE %I OWNER TO selltrack_migrator', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO selltrack_app, selltrack_backup', current_database()) \gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO selltrack_app;

-- Reading everything, including whatever gets added later, and nothing else.
GRANT pg_read_all_data TO selltrack_backup;

-- Whatever the migrator creates from now on.
ALTER DEFAULT PRIVILEGES FOR ROLE selltrack_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO selltrack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE selltrack_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO selltrack_app;

-- Whatever it has created already.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO selltrack_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO selltrack_app;

-- The exceptions. When an account is deleted, its entries in the audit log lose their link to it,
-- which still works: foreign key actions run with the permissions of the table's owner.
SELECT 'REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM selltrack_app'
WHERE to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM selltrack_app'
WHERE to_regclass('public.alembic_version') IS NOT NULL
\gexec
