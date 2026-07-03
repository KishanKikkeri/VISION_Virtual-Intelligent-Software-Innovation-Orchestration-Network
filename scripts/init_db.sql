-- AASC Database initialisation
-- Run once when PostgreSQL container first starts.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
