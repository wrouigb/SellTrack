-- For local development only: the role and databases the README and the tests expect. As in
-- production, the app isn't a superuser, so a test can't pass only because it was allowed anything.
CREATE ROLE selltrack LOGIN PASSWORD 'selltrack';
CREATE DATABASE selltrack_dev OWNER selltrack;
CREATE DATABASE selltrack_test OWNER selltrack;
