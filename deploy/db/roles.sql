-- The platform's database roles. Run by the db-init service as the bootstrap
-- superuser on every `make up`; safe to run again.
--
--   owner: owns the schema's objects and runs migrations. Not a superuser, so
--          no COPY ... PROGRAM and no reach past this database's own tables.
--   app:   what api, worker and internal-api connect as: reads and writes rows,
--          changes no schema.
--
-- The superuser itself stays only in the db and db-init containers.
--
-- psql variables: db, owner, app. Passwords come from the environment, not
-- the command line, which any host user can read in `ps`.

\set ON_ERROR_STOP on
\getenv owner_pw POSTGRES_OWNER_PASSWORD
\getenv app_pw POSTGRES_APP_PASSWORD

SELECT format('CREATE ROLE %I', :'owner')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT format('CREATE ROLE %I', :'app')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app') \gexec

ALTER ROLE :"owner" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'owner_pw';
ALTER ROLE :"app" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'app_pw';

GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"owner", :"app";
GRANT USAGE, CREATE ON SCHEMA public TO :"owner";
GRANT USAGE ON SCHEMA public TO :"app";

-- Objects made before these roles existed belong to the superuser. Tables
-- first: moving a table moves the sequences its columns own, which cannot be
-- moved on their own.
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
              n.nspname, c.relname, :'owner')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
  AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', n.nspname, c.relname, :'owner')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'S'
  AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :'owner') \gexec
-- Enums and other standalone types, which migrations alter.
SELECT format('ALTER TYPE %I.%I OWNER TO %I', n.nspname, t.typname, :'owner')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd')
  AND t.typowner <> (SELECT oid FROM pg_roles WHERE rolname = :'owner') \gexec
SELECT format('ALTER ROUTINE %s OWNER TO %I', p.oid::regprocedure, :'owner')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = :'owner') \gexec

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"app";
