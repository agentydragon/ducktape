// Package schema_transfer copies only the Connection and OAuth records into the MCP edge database.
package schema_transfer

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"

	_ "github.com/lib/pq"
)

const (
	actionsHistoryTable = "action_service_goose_version"
	edgeHistoryTable    = "mcp_edge_goose_version"
	transferLockID      = int64(0x4D43_5045_5452_414E)
)

type column struct {
	name     string
	typeName string
}

type table struct {
	name       string
	primaryKey string
	columns    []column
}

var tables = []table{
	{
		name: "external_connection", primaryKey: "id",
		columns: []column{
			{"id", "uuid"},
			{"display_name", "text"},
			{"version", "integer"},
			{"created_at", "timestamp with time zone"},
			{"updated_at", "timestamp with time zone"},
			{"bound_caller", "jsonb"},
			{"binding_version", "integer"},
		},
	},
	{
		name: "external_connection_grant", primaryKey: "id",
		columns: []column{
			{"id", "uuid"},
			{"connection_id", "uuid"},
			{"revision", "integer"},
			{"issuer", "text"},
			{"client_id", "text"},
			{"status", "text"},
			{"created_at", "timestamp with time zone"},
			{"activated_at", "timestamp with time zone"},
			{"revoked_at", "timestamp with time zone"},
			{"original_caller", "jsonb"},
		},
	},
	{
		name: "connection_enrollment", primaryKey: "id",
		columns: []column{
			{"id", "uuid"},
			{"handle_hash", "text"},
			{"issuer", "text"},
			{"client_id", "text"},
			{"client_name", "text"},
			{"redirect_uri", "text"},
			{"code_challenge", "text"},
			{"upstream_url", "text"},
			{"expires_at", "timestamp with time zone"},
			{"version", "integer"},
			{"operator_issuer", "text"},
			{"operator_subject", "text"},
			{"verdict", "text"},
			{"display_name", "text"},
			{"exchange_claimed_at", "timestamp with time zone"},
			{"connection_id", "uuid"},
			{"connection_version", "integer"},
			{"caller", "jsonb"},
		},
	},
	{
		name: "oauth_registered_client", primaryKey: "client_id",
		columns: []column{{"client_id", "text"}, {"metadata", "jsonb"}},
	},
	{
		name: "oauth_authorization", primaryKey: "id",
		columns: []column{
			{"id", "uuid"},
			{"client_id", "text"},
			{"redirect_uri", "text"},
			{"code_challenge", "text"},
			{"state", "text"},
			{"expires_at", "timestamp with time zone"},
			{"upstream_state_hash", "text"},
			{"upstream_verifier", "text"},
			{"code_hash", "text"},
			{"signed_in_at", "timestamp with time zone"},
			{"upstream_expires_at", "timestamp with time zone"},
			{"upstream_refresh_token", "text"},
			{"exchanged_at", "timestamp with time zone"},
		},
	},
	{
		name: "oauth_token", primaryKey: "token_hash",
		columns: []column{
			{"token_hash", "text"},
			{"authorization_id", "uuid"},
			{"kind", "text"},
			{"expires_at", "timestamp with time zone"},
			{"rotated_at", "timestamp with time zone"},
		},
	},
}

var lockOrder = []string{
	"connection_enrollment",
	"external_connection",
	"external_connection_grant",
	"oauth_authorization",
	"oauth_registered_client",
	"oauth_token",
}

// Result contains row counts only. Row values, token hashes, and connection secrets are never returned.
type Result struct {
	Rows map[string]int64
}

type failure struct {
	message string
	cause   error
}

func (e *failure) Error() string { return e.message }
func (e *failure) Unwrap() error { return e.cause }

func fail(message string, cause error) error {
	return &failure{message: message, cause: cause}
}

// Run copies the supported Connection and OAuth rows to an already migrated, empty edge database.
// Callers must stop Actions and keep its Connection and OAuth writers disabled until it can no longer
// serve those APIs or write these tables. The source locks prevent writes from changing copied rows
// while this function runs; they cannot replace that service-level fence.
func Run(ctx context.Context, actionsURL, edgeURL string) (Result, error) {
	if actionsURL == "" || edgeURL == "" {
		return Result{}, fail("both database URLs are required", nil)
	}
	actionsDB, err := sql.Open("postgres", actionsURL)
	if err != nil {
		return Result{}, fail("could not open the Actions database", err)
	}
	defer actionsDB.Close()
	actionsDB.SetMaxOpenConns(1)
	if err := actionsDB.PingContext(ctx); err != nil {
		return Result{}, fail("could not connect to the Actions database", err)
	}

	edgeDB, err := sql.Open("postgres", edgeURL)
	if err != nil {
		return Result{}, fail("could not open the MCP edge database", err)
	}
	defer edgeDB.Close()
	edgeDB.SetMaxOpenConns(1)
	if err := edgeDB.PingContext(ctx); err != nil {
		return Result{}, fail("could not connect to the MCP edge database", err)
	}

	actionsName, err := databaseName(ctx, actionsDB)
	if err != nil {
		return Result{}, fail("could not identify the Actions database", err)
	}
	edgeName, err := databaseName(ctx, edgeDB)
	if err != nil {
		return Result{}, fail("could not identify the MCP edge database", err)
	}
	if actionsName == edgeName {
		return Result{}, fail("source and destination must be different databases", nil)
	}

	edgeTx, err := edgeDB.BeginTx(ctx, nil)
	if err != nil {
		return Result{}, fail("could not begin the destination transaction", err)
	}
	defer edgeTx.Rollback()
	if _, err := edgeTx.ExecContext(ctx, "SET LOCAL search_path TO public"); err != nil {
		return Result{}, fail("could not configure the destination transaction", err)
	}
	if _, err := edgeTx.ExecContext(ctx, "SET LOCAL TIME ZONE 'UTC'"); err != nil {
		return Result{}, fail("could not configure the destination transaction", err)
	}
	if _, err := edgeTx.ExecContext(ctx, "SET LOCAL DateStyle = 'ISO, YMD'"); err != nil {
		return Result{}, fail("could not configure the destination transaction", err)
	}
	if _, err := edgeTx.ExecContext(ctx, "SET LOCAL lock_timeout = '5s'"); err != nil {
		return Result{}, fail("could not configure the destination lock timeout", err)
	}
	var ignored any
	if err := edgeTx.QueryRowContext(ctx, "SELECT pg_advisory_xact_lock($1)", transferLockID).Scan(&ignored); err != nil {
		return Result{}, fail("could not lock the destination for transfer", err)
	}
	if err := lockTables(ctx, edgeTx, append(append([]string{}, lockOrder...), edgeHistoryTable)); err != nil {
		return Result{}, fail("could not lock the destination schema", err)
	}
	if err := requireOnlyEdgeMigration(ctx, edgeTx); err != nil {
		return Result{}, fail("destination Goose migration 1 is not applied", err)
	}
	if err := requireNoExtraRelations(ctx, edgeTx); err != nil {
		return Result{}, fail("destination contains unexpected database objects", err)
	}
	for _, spec := range tables {
		count, err := rowCount(ctx, edgeTx, spec.name)
		if err != nil {
			return Result{}, fail("could not inspect the destination tables", err)
		}
		if count != 0 {
			return Result{}, fail("destination is not empty", nil)
		}
	}

	actionsTx, err := actionsDB.BeginTx(ctx, nil)
	if err != nil {
		return Result{}, fail("could not begin the source snapshot", err)
	}
	defer actionsTx.Rollback()
	if _, err := actionsTx.ExecContext(ctx, "SET LOCAL search_path TO public"); err != nil {
		return Result{}, fail("could not configure the source snapshot", err)
	}
	if _, err := actionsTx.ExecContext(ctx, "SET LOCAL TIME ZONE 'UTC'"); err != nil {
		return Result{}, fail("could not configure the source snapshot", err)
	}
	if _, err := actionsTx.ExecContext(ctx, "SET LOCAL DateStyle = 'ISO, YMD'"); err != nil {
		return Result{}, fail("could not configure the source snapshot", err)
	}
	if _, err := actionsTx.ExecContext(ctx, "SET LOCAL lock_timeout = '5s'"); err != nil {
		return Result{}, fail("could not configure the source lock timeout", err)
	}
	if err := lockTables(ctx, actionsTx, append(append([]string{}, lockOrder...), actionsHistoryTable)); err != nil {
		return Result{}, fail("could not fence source writes", err)
	}
	if err := requireKnownActionsMigrations(ctx, actionsTx); err != nil {
		return Result{}, fail("source Actions migration history is unsupported", err)
	}
	if err := requireMatchingSchemas(ctx, actionsTx, edgeTx); err != nil {
		return Result{}, fail("source schema does not match the MCP edge schema", err)
	}

	result := Result{Rows: make(map[string]int64, len(tables))}
	for _, spec := range tables {
		count, err := copyTable(ctx, actionsTx, edgeTx, spec)
		if err != nil {
			return Result{}, fail("could not copy the supported Connection and OAuth tables", err)
		}
		result.Rows[spec.name] = count
	}
	for _, spec := range tables {
		if err := compareTableRows(ctx, actionsTx, edgeTx, spec); err != nil {
			return Result{}, fail("destination row verification failed", err)
		}
	}
	if err := edgeTx.Commit(); err != nil {
		return Result{}, fail("destination commit outcome is uncertain; inspect it before retrying", err)
	}
	if err := actionsTx.Commit(); err != nil {
		return Result{}, fail("destination committed; source lock cleanup failed, inspect destination before retrying", err)
	}
	return result, nil
}

func databaseName(ctx context.Context, db *sql.DB) (string, error) {
	var name string
	err := db.QueryRowContext(ctx, "SELECT current_database()").Scan(&name)
	return name, err
}

func lockTables(ctx context.Context, tx *sql.Tx, names []string) error {
	for _, name := range names {
		if _, err := tx.ExecContext(ctx, "LOCK TABLE public."+name+" IN SHARE MODE"); err != nil {
			return err
		}
	}
	return nil
}

func requireOnlyEdgeMigration(ctx context.Context, tx *sql.Tx) error {
	rows, err := tx.QueryContext(ctx,
		"SELECT version_id, is_applied FROM public."+edgeHistoryTable+" ORDER BY version_id")
	if err != nil {
		return err
	}
	defer rows.Close()
	var count int
	seen := map[int64]bool{}
	for rows.Next() {
		var version int64
		var applied bool
		if err := rows.Scan(&version, &applied); err != nil {
			return err
		}
		if (version != 0 && version != 1) || !applied || seen[version] {
			return errors.New("destination migration history is unsupported")
		}
		seen[version] = true
		count++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if count != 2 || !seen[0] || !seen[1] {
		return errors.New("destination migration history must contain only applied versions 0 and 1")
	}
	return nil
}

func requireKnownActionsMigrations(ctx context.Context, tx *sql.Tx) error {
	rows, err := tx.QueryContext(ctx,
		"SELECT version_id, is_applied FROM public."+actionsHistoryTable+" ORDER BY version_id")
	if err != nil {
		return err
	}
	defer rows.Close()
	seen := map[int64]bool{}
	for rows.Next() {
		var version int64
		var applied bool
		if err := rows.Scan(&version, &applied); err != nil {
			return err
		}
		if version < 0 || version > 5 || !applied || seen[version] {
			return errors.New("source migration history is unsupported")
		}
		seen[version] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for version := int64(0); version <= 4; version++ {
		if !seen[version] {
			return errors.New("source is missing a required migration")
		}
	}
	return nil
}

func requireNoExtraRelations(ctx context.Context, tx *sql.Tx) error {
	allowed := []string{
		"mcp_edge_goose_version", "external_connection", "external_connection_grant", "connection_enrollment",
		"oauth_registered_client", "oauth_authorization", "oauth_token",
	}
	allowedSequences := map[string]struct{}{"public.mcp_edge_goose_version_id_seq": {}}
	rows, err := tx.QueryContext(ctx, `
		SELECT n.nspname, c.relname
		FROM pg_catalog.pg_class AS c
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
		  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
		  AND n.nspname NOT LIKE 'pg_toast%'
		ORDER BY n.nspname, c.relname`)
	if err != nil {
		return err
	}
	defer rows.Close()
	allowedSet := make(map[string]struct{}, len(allowed))
	for _, name := range allowed {
		allowedSet["public."+name] = struct{}{}
	}
	for rows.Next() {
		var schema, name string
		if err := rows.Scan(&schema, &name); err != nil {
			return err
		}
		if _, ok := allowedSequences[schema+"."+name]; ok {
			continue
		}
		if _, ok := allowedSet[schema+"."+name]; !ok {
			return errors.New("unexpected user relation exists")
		}
		delete(allowedSet, schema+"."+name)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(allowedSet) != 0 {
		return errors.New("expected destination relation is missing")
	}
	return nil
}

func rowCount(ctx context.Context, tx *sql.Tx, tableName string) (int64, error) {
	var count int64
	err := tx.QueryRowContext(ctx, "SELECT count(*) FROM public."+tableName).Scan(&count)
	return count, err
}

type columnShape struct {
	name       string
	typeName   string
	notNull    bool
	defaultSQL string
	identity   string
	generated  string
	collation  string
}

type constraintShape struct {
	typeName   string
	definition string
	validated  bool
	deferrable bool
	deferred   bool
}

type tableShape struct {
	kind             string
	rowSecurity      bool
	forceRowSecurity bool
	hasInheritance   bool
	columns          []columnShape
	constraints      []constraintShape
	indexes          []string
	triggers         []string
	policies         []string
}

func readShape(ctx context.Context, tx *sql.Tx, tableName string) (tableShape, error) {
	shape := tableShape{}
	columnRows, err := tx.QueryContext(ctx, `
		SELECT c.relkind::text, c.relrowsecurity, c.relforcerowsecurity,
		       EXISTS (SELECT 1 FROM pg_catalog.pg_inherits AS i WHERE i.inhrelid = c.oid OR i.inhparent = c.oid),
		       a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
		       COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''),
	       a.attidentity::text, a.attgenerated::text, COALESCE(coll.collname, '')
		FROM pg_catalog.pg_class AS c
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
		LEFT JOIN pg_catalog.pg_attrdef AS d ON d.adrelid = c.oid AND d.adnum = a.attnum
		LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = a.attcollation AND a.attcollation <> 0
		WHERE n.nspname = 'public' AND c.relname = $1 AND a.attnum > 0 AND NOT a.attisdropped
		ORDER BY a.attnum`, tableName)
	if err != nil {
		return tableShape{}, err
	}
	for columnRows.Next() {
		var column columnShape
		if err := columnRows.Scan(
			&shape.kind, &shape.rowSecurity, &shape.forceRowSecurity, &shape.hasInheritance,
			&column.name, &column.typeName, &column.notNull, &column.defaultSQL,
			&column.identity, &column.generated, &column.collation,
		); err != nil {
			columnRows.Close()
			return tableShape{}, err
		}
		shape.columns = append(shape.columns, column)
	}
	if err := columnRows.Err(); err != nil {
		columnRows.Close()
		return tableShape{}, err
	}
	if err := columnRows.Close(); err != nil {
		return tableShape{}, err
	}
	if shape.kind != "r" || len(shape.columns) == 0 || shape.rowSecurity || shape.forceRowSecurity || shape.hasInheritance {
		return tableShape{}, errors.New("table shape is not a plain, readable table")
	}

	constraintRows, err := tx.QueryContext(ctx, `
		SELECT con.contype::text, pg_catalog.pg_get_constraintdef(con.oid, false),
		       con.convalidated, con.condeferrable, con.condeferred
		FROM pg_catalog.pg_constraint AS con
		JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		WHERE n.nspname = 'public' AND c.relname = $1
		ORDER BY con.contype, pg_catalog.pg_get_constraintdef(con.oid, false)`, tableName)
	if err != nil {
		return tableShape{}, err
	}
	for constraintRows.Next() {
		var constraint constraintShape
		if err := constraintRows.Scan(
			&constraint.typeName, &constraint.definition, &constraint.validated,
			&constraint.deferrable, &constraint.deferred,
		); err != nil {
			constraintRows.Close()
			return tableShape{}, err
		}
		shape.constraints = append(shape.constraints, constraint)
	}
	if err := constraintRows.Err(); err != nil {
		constraintRows.Close()
		return tableShape{}, err
	}
	if err := constraintRows.Close(); err != nil {
		return tableShape{}, err
	}

	indexRows, err := tx.QueryContext(ctx, `
		SELECT ix.indisunique, ix.indisprimary, ix.indisvalid, ix.indisready,
		       pg_catalog.pg_get_indexdef(ix.indexrelid)
		FROM pg_catalog.pg_index AS ix
		JOIN pg_catalog.pg_class AS c ON c.oid = ix.indrelid
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		WHERE n.nspname = 'public' AND c.relname = $1
		ORDER BY pg_catalog.pg_get_indexdef(ix.indexrelid)`, tableName)
	if err != nil {
		return tableShape{}, err
	}
	for indexRows.Next() {
		var unique, primary, valid, ready bool
		var definition string
		if err := indexRows.Scan(&unique, &primary, &valid, &ready, &definition); err != nil {
			indexRows.Close()
			return tableShape{}, err
		}
		_, suffix, ok := strings.Cut(definition, " ON ")
		if !ok {
			indexRows.Close()
			return tableShape{}, errors.New("could not inspect a table index")
		}
		shape.indexes = append(shape.indexes, fmt.Sprintf("%t|%t|%t|%t|%s", unique, primary, valid, ready, suffix))
	}
	if err := indexRows.Err(); err != nil {
		indexRows.Close()
		return tableShape{}, err
	}
	if err := indexRows.Close(); err != nil {
		return tableShape{}, err
	}
	sort.Strings(shape.indexes)

	triggerRows, err := tx.QueryContext(ctx, `
		SELECT pg_catalog.pg_get_triggerdef(t.oid, false)
		FROM pg_catalog.pg_trigger AS t
		JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		WHERE n.nspname = 'public' AND c.relname = $1 AND NOT t.tgisinternal
		ORDER BY pg_catalog.pg_get_triggerdef(t.oid, false)`, tableName)
	if err != nil {
		return tableShape{}, err
	}
	for triggerRows.Next() {
		var trigger string
		if err := triggerRows.Scan(&trigger); err != nil {
			triggerRows.Close()
			return tableShape{}, err
		}
		shape.triggers = append(shape.triggers, trigger)
	}
	if err := triggerRows.Err(); err != nil {
		triggerRows.Close()
		return tableShape{}, err
	}
	if err := triggerRows.Close(); err != nil {
		return tableShape{}, err
	}

	policyRows, err := tx.QueryContext(ctx, `
		SELECT pol.polname, COALESCE(pg_catalog.pg_get_expr(pol.polqual, pol.polrelid), ''),
		       COALESCE(pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid), '')
		FROM pg_catalog.pg_policy AS pol
		JOIN pg_catalog.pg_class AS c ON c.oid = pol.polrelid
		JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
		WHERE n.nspname = 'public' AND c.relname = $1
		ORDER BY pol.polname`, tableName)
	if err != nil {
		return tableShape{}, err
	}
	for policyRows.Next() {
		var name, using, check string
		if err := policyRows.Scan(&name, &using, &check); err != nil {
			policyRows.Close()
			return tableShape{}, err
		}
		shape.policies = append(shape.policies, name+"|"+using+"|"+check)
	}
	if err := policyRows.Err(); err != nil {
		policyRows.Close()
		return tableShape{}, err
	}
	if err := policyRows.Close(); err != nil {
		return tableShape{}, err
	}
	return shape, nil
}

func requireMatchingSchemas(ctx context.Context, actionsTx, edgeTx *sql.Tx) error {
	for _, spec := range tables {
		actionsShape, err := readShape(ctx, actionsTx, spec.name)
		if err != nil {
			return err
		}
		edgeShape, err := readShape(ctx, edgeTx, spec.name)
		if err != nil {
			return err
		}
		if spec.name == "external_connection_grant" {
			actionsShape, err = withoutRetiredActivationDeadline(actionsShape)
			if err != nil {
				return err
			}
		}
		if !reflect.DeepEqual(actionsShape, edgeShape) {
			return errors.New("supported table shapes differ")
		}
	}
	return nil
}

func withoutRetiredActivationDeadline(shape tableShape) (tableShape, error) {
	filtered := make([]columnShape, 0, len(shape.columns))
	retiredColumns := 0
	for _, column := range shape.columns {
		if column.name != "activation_deadline" {
			filtered = append(filtered, column)
			continue
		}
		retiredColumns++
		if column.typeName != "timestamp with time zone" || column.notNull || column.defaultSQL != "" ||
			column.identity != "" || column.generated != "" || column.collation != "" {
			return tableShape{}, errors.New("retired source column has an unsupported definition")
		}
	}
	if retiredColumns > 1 {
		return tableShape{}, errors.New("retired source column is ambiguous")
	}
	shape.columns = filtered
	return shape, nil
}

func copyTable(ctx context.Context, source, target *sql.Tx, spec table) (int64, error) {
	rows, err := source.QueryContext(ctx, selectRowsSQL(spec))
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	var count int64
	for rows.Next() {
		values, err := scanTextRow(rows, len(spec.columns))
		if err != nil {
			return 0, err
		}
		result, err := target.ExecContext(ctx, insertRowSQL(spec), values...)
		if err != nil {
			return 0, err
		}
		inserted, err := result.RowsAffected()
		if err != nil {
			return 0, err
		}
		if inserted != 1 {
			return 0, errors.New("row insert count was unexpected")
		}
		count++
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	if err := rows.Close(); err != nil {
		return 0, err
	}
	return count, nil
}

func compareTableRows(ctx context.Context, source, target *sql.Tx, spec table) error {
	sourceRows, err := source.QueryContext(ctx, selectRowsSQL(spec))
	if err != nil {
		return err
	}
	defer sourceRows.Close()
	targetRows, err := target.QueryContext(ctx, selectRowsSQL(spec))
	if err != nil {
		return err
	}
	defer targetRows.Close()
	for {
		sourceRow, sourceOK, err := nextTextRow(sourceRows, len(spec.columns))
		if err != nil {
			return err
		}
		targetRow, targetOK, err := nextTextRow(targetRows, len(spec.columns))
		if err != nil {
			return err
		}
		if sourceOK != targetOK || !reflect.DeepEqual(sourceRow, targetRow) {
			return errors.New("rows do not match")
		}
		if !sourceOK {
			break
		}
	}
	return sourceRows.Err()
}

func selectRowsSQL(spec table) string {
	columns := make([]string, len(spec.columns))
	for i, column := range spec.columns {
		columns[i] = column.name + "::text"
	}
	return "SELECT " + strings.Join(columns, ", ") + " FROM public." + spec.name + " ORDER BY " + spec.primaryKey
}

func insertRowSQL(spec table) string {
	columns := make([]string, len(spec.columns))
	values := make([]string, len(spec.columns))
	for i, column := range spec.columns {
		columns[i] = column.name
		values[i] = fmt.Sprintf("$%d::%s", i+1, column.typeName)
	}
	return "INSERT INTO public." + spec.name + " (" + strings.Join(columns, ", ") + ") VALUES (" +
		strings.Join(values, ", ") + ")"
}

func scanTextRow(rows *sql.Rows, columnCount int) ([]any, error) {
	values := make([]sql.NullString, columnCount)
	destinations := make([]any, columnCount)
	for i := range values {
		destinations[i] = &values[i]
	}
	if err := rows.Scan(destinations...); err != nil {
		return nil, err
	}
	row := make([]any, columnCount)
	for i, value := range values {
		if value.Valid {
			row[i] = value.String
		}
	}
	return row, nil
}

func nextTextRow(rows *sql.Rows, columnCount int) ([]any, bool, error) {
	if !rows.Next() {
		return nil, false, rows.Err()
	}
	row, err := scanTextRow(rows, columnCount)
	return row, true, err
}
