"""Admin repository — admin-specific queries and operations."""

from __future__ import annotations

from loguru import logger

from repositories.base import BaseRepository

# Issue #21 — registre des caches géo visibles/purgeables depuis l'admin.
# Chaque entrée suit le même pattern de table (`area_key` en clé, une valeur
# identifiant nullable, `resolved_at`) : `colonne` porte le nom de la valeur
# résolue, dont NULL mémorise un ÉCHEC de résolution. Une nouvelle source qui
# ajoute son cache doit s'enregistrer ICI (et dans la fixture clean_geo_cache
# des tests d'intégration) — rien d'autre.
# Cas particulier `commune_centres` (#26) : seuls les SUCCÈS y sont mémorisés,
# il n'y a donc pas de « manquée » à compter (suivi_echecs=False).
# Hors périmètre volontaire : transit_communes_rayon (#28, cache dérivé sans
# échec mémorisé, jamais NULL).
CACHES_GEO = (
    {"source": "seloger", "table": "seloger_place_ids", "colonne": "place_id", "suivi_echecs": True},
    {"source": "bienici", "table": "bienici_zone_ids", "colonne": "zone_ids", "suivi_echecs": True},
    {"source": "century21", "table": "century21_geo_ids", "colonne": "slug_id", "suivi_echecs": True},
    {"source": "orpi", "table": "orpi_geo_ids", "colonne": "slug_id", "suivi_echecs": True},
    {"source": "pap", "table": "pap_geo_ids", "colonne": "geo_id", "suivi_echecs": True},
    {"source": "foncia", "table": "foncia_geo_ids", "colonne": "slug_id", "suivi_echecs": True},
    {"source": "guyhoquet", "table": "guyhoquet_geo_ids", "colonne": "slug_id", "suivi_echecs": True},
    {"source": "citya", "table": "citya_geo_ids", "colonne": "slug_id", "suivi_echecs": True},
    {"source": "communes", "table": "commune_centres", "colonne": None, "suivi_echecs": False},
)


class AdminRepository(BaseRepository):
    """Admin-specific queries (stats, logs, DB management)."""

    def get_admin_stats(self) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM users) AS users,
                        (SELECT COUNT(*) FROM searches) AS searches,
                        (SELECT COUNT(*) FROM listings) AS listings,
                        (SELECT COUNT(*) FROM search_listings
                         WHERE found_at >= CURRENT_DATE) AS new_today
                """)
                row = cur.fetchone()
                return {
                    "users": row["users"],
                    "searches": row["searches"],
                    "total_listings": row["listings"],
                    "new_today": row["new_today"],
                }
        finally:
            self._release_conn(conn)

    def get_enhanced_admin_stats(self) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM users) AS users,
                        (SELECT COUNT(*) FROM searches) AS searches,
                        (SELECT COUNT(*) FROM listings) AS listings,
                        (SELECT COUNT(*) FROM search_listings) AS search_listings,
                        (SELECT COUNT(*) FROM search_listings
                         WHERE found_at >= CURRENT_DATE) AS new_today,
                        (SELECT COUNT(*) FROM listings l
                         LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                         WHERE sl.listing_id IS NULL) AS orphans,
                        (SELECT COALESCE(AVG(cnt), 0)
                         FROM (SELECT COUNT(*) AS cnt FROM search_listings GROUP BY search_id) sub) AS avg_listings,
                        (SELECT COUNT(*) FROM users
                         WHERE id NOT IN (SELECT DISTINCT user_id FROM searches)) AS users_no_searches
                """)
                counts = cur.fetchone()

                cur.execute(
                    """SELECT u.username, COUNT(DISTINCT sl.listing_id) AS listing_count
                       FROM users u
                       JOIN searches s ON s.user_id = u.id
                       JOIN search_listings sl ON sl.search_id = s.id
                       GROUP BY u.id, u.username
                       ORDER BY listing_count DESC LIMIT 5"""
                )
                top_users = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT s.id, s.label, u.username, COUNT(sl.listing_id) AS listing_count
                       FROM searches s
                       JOIN users u ON u.id = s.user_id
                       JOIN search_listings sl ON sl.search_id = s.id
                       GROUP BY s.id, u.username
                       ORDER BY listing_count DESC LIMIT 5"""
                )
                top_searches = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT DATE(sl.found_at) AS day, COUNT(DISTINCT sl.listing_id) AS count
                       FROM search_listings sl
                       WHERE sl.found_at >= CURRENT_DATE - INTERVAL '7 days'
                       GROUP BY DATE(sl.found_at)
                       ORDER BY day"""
                )
                activity_7d = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT source, COUNT(*) AS cnt FROM searches
                       GROUP BY source ORDER BY cnt DESC"""
                )
                sources_breakdown = [dict(r) for r in cur.fetchall()]

            return {
                "users": counts["users"],
                "searches": counts["searches"],
                "total_listings": counts["listings"],
                "search_listings": counts["search_listings"],
                "new_today": counts["new_today"],
                "orphan_listings": counts["orphans"],
                "avg_listings_per_search": round(counts["avg_listings"], 1),
                "top_users": top_users,
                "top_searches": top_searches,
                "activity_7d": activity_7d,
                "sources_breakdown": sources_breakdown,
                "users_without_searches": counts["users_no_searches"],
            }
        finally:
            self._release_conn(conn)

    def get_geo_cache_stats(self) -> list[dict]:
        """Compteurs des caches géo, source par source (issue #21).

        Pour chaque cache du registre : `entrees` (COUNT(*)) et `manquees`
        (lignes où l'identifiant résolu est NULL — un échec MÉMORISÉ, à
        distinguer de « jamais tenté », cf. les repos *_geo_repo). Les noms
        de tables viennent du registre CACHES_GEO, jamais d'une entrée
        utilisateur : l'interpolation f-string est donc sûre par construction.
        """
        results = []
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                for cache in CACHES_GEO:
                    if cache["colonne"]:
                        cur.execute(
                            f"SELECT COUNT(*) AS entrees,"
                            f" COUNT(*) - COUNT({cache['colonne']}) AS manquees"
                            f" FROM {cache['table']}"
                        )
                    else:
                        # Sans colonne identifiable : pas d'échec mémorisable.
                        cur.execute(f"SELECT COUNT(*) AS entrees, 0 AS manquees FROM {cache['table']}")
                    row = dict(cur.fetchone())
                    row.update({"source": cache["source"], "table": cache["table"],
                                "suivi_echecs": cache["suivi_echecs"]})
                    results.append(row)
            return results
        finally:
            self._release_conn(conn)

    def purge_geo_cache(self, table: str) -> int:
        """Vide UN cache géo identifié par sa table (issue #21).

        L'allowlist CACHES_GEO est la seule protection : le nom de table est
        interpolé dans le DELETE, toute valeur hors registre lève ValueError
        AVANT de toucher à la base (jamais une erreur Postgres brute).
        Retourne le nombre de lignes réellement supprimées.
        """
        if table not in {c["table"] for c in CACHES_GEO}:
            raise ValueError(f"Cache géo inconnu : '{table}'")
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table}")
                deleted = cur.rowcount
                conn.commit()
                logger.info(f"Purgé {deleted} entrée(s) du cache géo {table}")
                return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def log_admin_action(self, action: str, details: str = "", performed_by: str = "") -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO admin_logs (action, details, performed_by) VALUES (%s, %s, %s)",
                    (action, details, performed_by),
                )
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to log admin action: {e}")
        finally:
            self._release_conn(conn)

    def get_admin_logs(self, limit=50, offset=0, action_filter="", date_from="", date_to="") -> list[dict]:
        query = "SELECT * FROM admin_logs"
        conditions = []
        params = []

        if action_filter:
            conditions.append("action = %s")
            params.append(action_filter)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            params.append(date_to)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_admin_logs(self, action_filter="", date_from="", date_to="") -> int:
        query = "SELECT COUNT(*) AS cnt FROM admin_logs"
        conditions = []
        params = []

        if action_filter:
            conditions.append("action = %s")
            params.append(action_filter)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            params.append(date_to)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def purge_old_logs(self, days: int = 30) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM admin_logs WHERE created_at < NOW() - make_interval(days => %s)",
                    (days,),
                )
                conn.commit()
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Purgé {deleted} anciens logs admin")
            return deleted
        finally:
            self._release_conn(conn)

    def get_db_stats(self) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())) AS size")
                db_size = cur.fetchone()["size"]

                cur.execute(
                    """SELECT relname AS table_name,
                              n_live_tup AS row_count,
                              pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                              pg_size_pretty(pg_relation_size(relid)) AS data_size
                       FROM pg_stat_user_tables
                       ORDER BY pg_total_relation_size(relid) DESC"""
                )
                tables = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT schemaname, tablename, indexname,
                              pg_size_pretty(pg_relation_size(schemaname || '.' || indexname)) AS index_size
                       FROM pg_indexes
                       WHERE schemaname = 'public'
                       ORDER BY tablename, indexname"""
                )
                indexes = [dict(r) for r in cur.fetchall()]

            return {
                "db_size": db_size,
                "tables": tables,
                "indexes": indexes,
            }
        finally:
            self._release_conn(conn)

    def get_table_details(self, table_name: str) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT column_name, data_type, is_nullable, column_default,
                              character_maximum_length
                       FROM information_schema.columns
                       WHERE table_name = %s AND table_schema = 'public'
                       ORDER BY ordinal_position""",
                    (table_name,),
                )
                columns = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT indexname, indexdef
                       FROM pg_indexes
                       WHERE tablename = %s AND schemaname = 'public'""",
                    (table_name,),
                )
                indexes = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT conname AS constraint_name, contype AS constraint_type,
                              pg_get_constraintdef(oid) AS definition
                       FROM pg_constraint
                       WHERE conrelid = %s::regclass""",
                    (table_name,),
                )
                constraints = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    "SELECT pg_size_pretty(pg_total_relation_size(%s::regclass)) AS total_size",
                    (table_name,),
                )
                size_row = cur.fetchone()
                total_size = size_row["total_size"] if size_row else "N/A"

            return {
                "columns": columns,
                "indexes": indexes,
                "constraints": constraints,
                "total_size": total_size,
            }
        finally:
            self._release_conn(conn)

    def execute_query(self, sql: str) -> tuple:
        """Execute a read-only SQL query. Returns (rows, row_count, error).

        Runs inside a READ ONLY transaction that is always rolled back, so
        this endpoint cannot be used to mutate data (INSERT/UPDATE/DELETE/DDL
        are rejected by Postgres before they can take effect).
        """
        conn = self._get_conn_for_request()
        try:
            conn.rollback()
            with self._dict_cursor(conn) as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(sql)
                if cur.description:
                    rows = cur.fetchall()
                    row_count = cur.rowcount
                    result = [dict(r) for r in rows], row_count, None
                else:
                    result = [], cur.rowcount, None
            conn.rollback()
            return result
        except Exception as e:
            conn.rollback()
            return [], 0, str(e)
        finally:
            self._release_conn(conn)

    def get_active_connections(self) -> list[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT pid, usename, application_name, client_addr,
                              backend_start, state, query, query_start
                       FROM pg_stat_activity
                       WHERE datname = current_database() AND pid != pg_backend_pid()
                       ORDER BY backend_start"""
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def truncate_table(self, table_name: str) -> bool:
        ALLOWED_TABLES = {
            "users", "searches", "listings", "search_listings",
            "scrape_logs", "admin_logs", "app_settings",
        }
        if table_name not in ALLOWED_TABLES:
            logger.error(f"Truncate refused: table '{table_name}' non autorisée")
            return False
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {table_name} CASCADE")
                conn.commit()
                return True
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to truncate {table_name}: {e}")
            return False
        finally:
            self._release_conn(conn)
