"""Listing repository — CRUD for listings."""

from __future__ import annotations

from datetime import date

import psycopg2
import psycopg2.extras
from loguru import logger
from psycopg2.extras import execute_values

from parsers._dates import DATE_INCONNUE
from repositories.base import BaseRepository


def _borne_date_comparee(valeur: str, *, fin_de_journee: bool) -> str:
    """Valide une borne de filtre de dates et la rend lexicalement comparable.

    Issue #12 : ``creation_date`` est stocké en ISO-8601 UTC canonique
    (``YYYY-MM-DDTHH:MM:SS+00:00``) — la comparaison lexicale n'est fiable
    que si la borne est elle-même au même format. La route transmet une
    date seule (« AAAA-MM-JJ », input type=date) : on l'étend en borne de
    journée complète (début pour date_min, fin pour date_max) afin que le
    jour borne soit INCLUS des deux côtés.

    Lève ValueError si le format n'est pas une date valide : un filtre
    malformé ne doit jamais produire un filtrage silencieusement faux.
    """
    try:
        # date.fromisoformat (et non strptime) : pas de datetime naïf
        # construit ici, seule la composante calendaire nous intéresse —
        # la borne est ensuite étendue explicitement en UTC canonique.
        jour = date.fromisoformat(valeur.strip()).isoformat()
    except ValueError as erreur:
        raise ValueError(
            f"Borne de date invalide (format attendu AAAA-MM-JJ) : « {valeur} »"
        ) from erreur
    return (
        f"{jour}T23:59:59+00:00" if fin_de_journee else f"{jour}T00:00:00+00:00"
    )


def _clean_string(s: str) -> str:
    """Remove surrogate characters that can't be encoded to UTF-8."""
    if s is None:
        return None
    return s.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


class ListingRepository(BaseRepository):
    """Listing CRUD operations."""

    def link_listing_to_search(self, search_id: int, listing_id: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO search_listings (search_id, listing_id) VALUES (%s, %s)",
                    (search_id, listing_id),
                )
                conn.commit()
                return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False
        finally:
            self._release_conn(conn)

    def save_and_link(self, listings, search_id: int) -> tuple:
        """Save listings and link them to a search — batch insert via execute_values.

        Returns:
            (new_listings, already_linked)
        """
        if not listings:
            return [], []

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                def clean(v):
                    return _clean_string(v) if isinstance(v, str) else v

                listing_data = [
                    (
                        item.listing_id, item.url, clean(item.title), clean(item.price), item.surface, item.rooms,
                        clean(item.location), clean(item.image_url), clean(item.description),
                        clean(item.agency), item.source,
                        item.legacy_id, item.price_value, clean(item.price_details),
                        clean(item.city), clean(item.district),
                        item.zip_code, clean(item.property_type), item.is_private, clean(item.phone),
                        item.epc, item.ges, item.is_new, item.is_exclusive, item.has_3d_visit,
                        item.creation_date, item.update_date, clean(item.headline), clean(item.photos),
                        # Issue #26 : géolocalisation hybride (native ou fallback
                        # commune) — NULL si la source n'a rien donné.
                        item.latitude, item.longitude, item.location_precision or None,
                    )
                    for item in listings
                ]

                execute_values(cur, """
                    INSERT INTO listings (
                        listing_id, url, title, price, surface, rooms, location, image_url,
                        description, agency, source, legacy_id, price_value, price_details,
                        city, district, zip_code, property_type, is_private, phone,
                        epc, ges, is_new, is_exclusive, has_3d_visit, creation_date,
                        update_date, headline, photos, latitude, longitude, location_precision
                    ) VALUES %s
                    ON CONFLICT (listing_id) DO NOTHING
                """, listing_data, page_size=100)

                # notified=FALSE explicitly on every new link, regardless of the
                # column's DEFAULT TRUE (which exists only to backfill pre-existing
                # rows as "already notified" when the column was introduced).
                link_data = [(search_id, item.listing_id, False) for item in listings]
                inserted = execute_values(cur, """
                    INSERT INTO search_listings (search_id, listing_id, notified)
                    VALUES %s
                    ON CONFLICT DO NOTHING
                    RETURNING listing_id
                """, link_data, page_size=100, fetch=True)

                conn.commit()

            newly_linked_ids = {row[0] for row in inserted}
            new_for_search = [item for item in listings if item.listing_id in newly_linked_ids]
            already_linked = [item for item in listings if item.listing_id not in newly_linked_ids]
            return new_for_search, already_linked
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def get_unnotified_listings_for_search(self, search_id: int) -> list:
        """Listings linked to a search but not yet successfully notified.

        Includes this run's new listings plus any left over from a previous
        scrape that crashed or failed to deliver the notification, so nothing
        is silently lost.
        """
        from models.listing import Listing

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT l.* FROM search_listings sl
                    JOIN listings l ON l.listing_id = sl.listing_id
                    WHERE sl.search_id = %s AND sl.notified = FALSE
                """, (search_id,))
                return [Listing.from_dict(dict(row)) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def mark_listings_notified(self, search_id: int, listing_ids: list[str]) -> None:
        """Mark listings as successfully notified for this search."""
        if not listing_ids:
            return
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE search_listings SET notified = TRUE "
                    "WHERE search_id = %s AND listing_id = ANY(%s)",
                    (search_id, listing_ids),
                )
                conn.commit()
        finally:
            self._release_conn(conn)

    def _build_filter_clauses(self, filters: dict, params: list, prefix: str = "l.") -> str:
        clauses = []
        if filters.get("q"):
            val = f"%{filters['q']}%"
            clauses.append(
                f"({prefix}title ILIKE %s OR {prefix}location ILIKE %s"
                f" OR {prefix}agency ILIKE %s OR {prefix}description ILIKE %s)"
            )
            params.extend([val, val, val, val])
        if filters.get("price_min") is not None:
            clauses.append(f"{prefix}price_value >= %s")
            params.append(filters["price_min"])
        if filters.get("price_max") is not None:
            clauses.append(f"{prefix}price_value <= %s")
            params.append(filters["price_max"])
        if filters.get("surface_min") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) >= %s")
            params.append(filters["surface_min"])
        if filters.get("surface_max") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) <= %s")
            params.append(filters["surface_max"])
        if filters.get("rooms_min") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}rooms, '[^0-9.]', '', 'g'), '') AS NUMERIC) >= %s")
            params.append(filters["rooms_min"])
        if filters.get("rooms_max") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}rooms, '[^0-9.]', '', 'g'), '') AS NUMERIC) <= %s")
            params.append(filters["rooms_max"])
        if filters.get("city"):
            clauses.append(f"{prefix}city ILIKE %s")
            params.append(f"%{filters['city']}%")
        if filters.get("district"):
            clauses.append(f"{prefix}district ILIKE %s")
            params.append(f"%{filters['district']}%")
        if filters.get("zip_code"):
            clauses.append(f"{prefix}zip_code = %s")
            params.append(filters["zip_code"])
        if filters.get("property_type"):
            clauses.append(f"{prefix}property_type = %s")
            params.append(filters["property_type"])
        if filters.get("agency"):
            clauses.append(f"{prefix}agency = %s")
            params.append(filters["agency"])
        if filters.get("epc"):
            clauses.append(f"{prefix}epc = %s")
            params.append(filters["epc"])
        if filters.get("ges"):
            clauses.append(f"{prefix}ges = %s")
            params.append(filters["ges"])
        if filters.get("is_private") is not None:
            clauses.append(f"{prefix}is_private = %s")
            params.append(filters["is_private"])
        if filters.get("is_new") is not None:
            clauses.append(f"{prefix}is_new = %s")
            params.append(filters["is_new"])
        if filters.get("date_min"):
            # Issue #12 : ni « unknown » ni un vide hérité n'est une date —
            # exclus explicitement ; la borne est validée puis étendue en
            # début de journée ISO UTC.
            clauses.append(f"{prefix}creation_date NOT IN ('{DATE_INCONNUE}', '')")
            clauses.append(f"{prefix}creation_date >= %s")
            params.append(_borne_date_comparee(filters["date_min"], fin_de_journee=False))
        if filters.get("date_max"):
            clauses.append(f"{prefix}creation_date NOT IN ('{DATE_INCONNUE}', '')")
            clauses.append(f"{prefix}creation_date <= %s")
            params.append(_borne_date_comparee(filters["date_max"], fin_de_journee=True))
        if filters.get("blacklisted_agencies"):
            placeholders = ",".join(["%s"] * len(filters["blacklisted_agencies"]))
            clauses.append(f"{prefix}agency NOT IN ({placeholders})")
            params.extend(filters["blacklisted_agencies"])
        if filters.get("first_seen_min"):
            # Issue #20 : ``first_seen`` est une vraie colonne TIMESTAMP (pas
            # du TEXT comme creation_date) — aucune sentinelle à exclure, et
            # Postgres caste le littéral ISO étendu en début de journée.
            clauses.append(f"{prefix}first_seen >= %s")
            params.append(_borne_date_comparee(filters["first_seen_min"], fin_de_journee=False))
        if filters.get("first_seen_max"):
            clauses.append(f"{prefix}first_seen <= %s")
            params.append(_borne_date_comparee(filters["first_seen_max"], fin_de_journee=True))
        if filters.get("orphans_only"):
            # Issue #20 : sémantique « orpheline » IDENTIQUE à celle de
            # get_orphan_listings_count / delete_orphan_listings (LEFT JOIN
            # … IS NULL ⇔ NOT EXISTS sur search_listings). Le préfixe qualifie
            # la colonne corrélée externe ; l'alias interne ne doit jamais
            # entrer en collision avec celui de la requête appelante.
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM search_listings orphan_sl"
                f" WHERE orphan_sl.listing_id = {prefix}listing_id)"
            )
        return " AND ".join(clauses)

    def _build_admin_order_clause(self, sort: str | None = None) -> str:
        """Allowlist des tris de la liste ADMIN globale (issue #20).

        Distincte de `_build_order_clause` : cette dernière porte des colonnes
        de la jointure search-scoped (`sl.found_at`) absentes de
        `get_all_listings` — partager la table produirait du SQL invalide dès
        qu'un onglet emprunterait le tri de l'autre. Le défaut reste EXACTEMENT
        celui d'avant l'issue (#20 non-régression) : première détection,
        plus récent en tête.
        """
        sort_map = {
            "prix_asc": "l.price_value ASC NULLS LAST",
            "prix_desc": "l.price_value DESC NULLS LAST",
            "date_asc": "l.first_seen ASC NULLS LAST",
            "date_desc": "l.first_seen DESC",
            "source_asc": "l.source ASC",
            "source_desc": "l.source DESC",
        }
        return sort_map.get(sort, "l.first_seen DESC")

    def _build_order_clause(self, sort: str = "found_at_desc") -> str:
        sort_map = {
            "found_at_desc": "sl.found_at DESC",
            "found_at_asc": "sl.found_at ASC",
            "price_asc": "l.price_value ASC NULLS LAST",
            "price_desc": "l.price_value DESC NULLS LAST",
            "surface_asc": "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) ASC NULLS LAST",
            "surface_desc": (
                "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) DESC NULLS LAST"
            ),
            # Issue #12 : les valeurs canoniques (ISO-8601 UTC) se trient
            # lexicalement ; les états dégénérés — sentinelle « unknown »,
            # vide hérité d'une base pas encore migrée, NULL défensif — sont
            # repoussés EN DERNIER dans les deux sens par un drapeau de tri
            # (NULLS LAST seul ne suffit plus : ce ne sont pas des NULL).
            # Au sein du compartiment dégénéré, l'ordre reste ASC dans les
            # DEUX sens : le tri desc ne doit pas inverser un ordre sans
            # sémantique chronologique. (Deux CASE sans ELSE : chaque clé
            # ne porte que son compartiment, l'autre vaut NULL partout.)
            "date_desc": (
                f"CASE WHEN l.creation_date IN ('{DATE_INCONNUE}', '') "
                "OR l.creation_date IS NULL THEN 1 ELSE 0 END ASC, "
                f"CASE WHEN l.creation_date IN ('{DATE_INCONNUE}', '') "
                "OR l.creation_date IS NULL THEN l.creation_date END ASC, "
                f"CASE WHEN l.creation_date IN ('{DATE_INCONNUE}', '') "
                "OR l.creation_date IS NULL THEN NULL ELSE l.creation_date END DESC"
            ),
            "date_asc": (
                f"CASE WHEN l.creation_date IN ('{DATE_INCONNUE}', '') "
                "OR l.creation_date IS NULL THEN 1 ELSE 0 END ASC, "
                "l.creation_date ASC"
            ),
        }
        return sort_map.get(sort, "sl.found_at DESC")

    def get_listings_for_search(self, search_id: int, limit: int = 50, offset: int = 0,
                                blacklisted_agencies: list[str] | None = None,
                                filters: dict | None = None, sort: str = "found_at_desc") -> list[dict]:
        effective_filters = dict(filters) if filters else {}
        if blacklisted_agencies:
            effective_filters["blacklisted_agencies"] = blacklisted_agencies

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                query = """SELECT l.*, sl.found_at
FROM listings l
JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s"""
                params = [search_id]

                filter_clauses = self._build_filter_clauses(effective_filters, params)
                if filter_clauses:
                    query += " AND " + filter_clauses

                query += f" ORDER BY {self._build_order_clause(sort)} LIMIT %s OFFSET %s"
                params.extend([limit, offset])

                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def get_map_points_for_search(self, search_id: int) -> list[dict]:
        """Les points carte d'une recherche (issue #26) : uniquement les
        annonces liées porteuses de coordonnées, avec le minimum de champs
        dont la popup a besoin.

        Une annonce sans coordonnées n'est pas une erreur — elle est
        simplement absente du résultat (criterion d'acceptation #26). Le
        filtrage se fait dans le SQL (`latitude IS NOT NULL`) : pas de ligne
        inutile transférée pour les recherches majoritairement non géoloc.
        """
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT l.listing_id, l.title, l.price, l.price_value,
                           l.surface, l.rooms, l.city, l.zip_code, l.url,
                           l.source, l.agency, l.latitude, l.longitude,
                           l.location_precision
                    FROM search_listings sl
                    JOIN listings l ON l.listing_id = sl.listing_id
                    WHERE sl.search_id = %s
                      AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL
                """, (search_id,))
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_listings_for_search(self, search_id: int, blacklisted_agencies: list[str] | None = None,
                                  filters: dict | None = None) -> int:
        effective_filters = dict(filters) if filters else {}
        if blacklisted_agencies:
            effective_filters["blacklisted_agencies"] = blacklisted_agencies

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                query = (
                    "SELECT COUNT(*) AS cnt FROM search_listings sl"
                    " JOIN listings l ON l.listing_id = sl.listing_id WHERE sl.search_id = %s"
                )
                params = [search_id]

                filter_clauses = self._build_filter_clauses(effective_filters, params)
                if filter_clauses:
                    query += " AND " + filter_clauses

                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def get_filter_options(self, search_id: int) -> dict:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                result = {}
                for col in ("city", "district", "zip_code", "property_type", "agency", "epc", "ges"):
                    cur.execute(
                        f"""SELECT DISTINCT l.{col}
FROM listings l
JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s AND l.{col} IS NOT NULL AND l.{col} != ''
ORDER BY l.{col}""",
                        (search_id,),
                    )
                    result[col] = [row[0] for row in cur.fetchall()]
                cur.execute(
                    """SELECT COUNT(*) FILTER (WHERE l.is_private = TRUE),
COUNT(*) FILTER (WHERE l.is_private = FALSE),
COUNT(*) FILTER (WHERE l.is_new = TRUE),
COUNT(*) FILTER (WHERE l.is_new = FALSE)
FROM listings l JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s""",
                    (search_id,),
                )
                row = cur.fetchone()
                result["has_private"] = row[0] > 0
                result["has_non_private"] = row[1] > 0
                result["has_new"] = row[2] > 0
                result["has_not_new"] = row[3] > 0
                return result
        finally:
            self._release_conn(conn)

    def delete_old_listings(self, days: int = 4) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM listings WHERE first_seen < NOW() - make_interval(days => %s)",
                    (days,),
                )
                conn.commit()
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Supprimé {deleted} anciennes annonces (>{days} jours)")
            return deleted
        finally:
            self._release_conn(conn)

    def delete_listing(self, listing_id: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM listings WHERE listing_id = %s", (listing_id,))
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def get_orphan_listings_count(self) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) AS cnt FROM listings l
                       LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                       WHERE sl.listing_id IS NULL"""
                )
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def delete_orphan_listings(self) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM listings WHERE listing_id IN (
                        SELECT l.listing_id FROM listings l
                        LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                        WHERE sl.listing_id IS NULL
                    )"""
                )
                conn.commit()
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Supprimé {deleted} annonces orphelines")
            return deleted
        finally:
            self._release_conn(conn)

    def _admin_listing_where(self, search_term: str, source_filter: str,
                             filters: dict | None, params: list) -> str:
        """WHERE partagé de la liste admin — LA garantie de parité.

        `get_all_listings` et `count_all_listings` reconstruisent chacune leur
        requête : si elles divergent, le compteur ment sur la pagination
        (page fantôme, lignes inatteignables). Un seul constructeur pour les
        deux, comme `_build_filter_clauses` l'impose côté search-scoped.
        """
        conditions = []
        if search_term:
            conditions.append("(l.title ILIKE %s OR l.location ILIKE %s OR l.description ILIKE %s)")
            params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])
        if source_filter:
            conditions.append("l.source = %s")
            params.append(source_filter)
        # Issue #20 : filtres avancés (prix, période de première détection,
        # orphelines) construits par le MÊME `_build_filter_clauses` que les
        # autres écrans — aucune clause SQL dupliquée.
        filter_clauses = self._build_filter_clauses(dict(filters) if filters else {}, params)
        if filter_clauses:
            conditions.append(filter_clauses)
        return (" WHERE " + " AND ".join(conditions)) if conditions else ""

    def get_all_listings(self, limit=50, offset=0, search_term="", source_filter="",
                         filters=None, sort=None) -> list[dict]:
        query = """SELECT l.*,
                          (SELECT COUNT(*) FROM search_listings WHERE listing_id = l.listing_id) AS linked_searches
                   FROM listings l"""
        params: list = []
        query += self._admin_listing_where(search_term, source_filter, filters, params)

        query += f" ORDER BY {self._build_admin_order_clause(sort)} LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_all_listings(self, search_term="", source_filter="", filters=None) -> int:
        query = "SELECT COUNT(DISTINCT l.listing_id) AS cnt FROM listings l"
        params: list = []
        query += self._admin_listing_where(search_term, source_filter, filters, params)

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def delete_listings(self, listing_ids: list[str]) -> int:
        """Suppression GROUPÉE (issue #20) — retourne le nombre RÉEL supprimé.

        Un seul DELETE paramétré (`= ANY(%s)` avec la liste en unique argument :
        aucun placeholder compté à la main, cf. mark_listings_notified). Les IDs
        absents (déjà supprimés entre-temps) réduisent naturellement le
        rowcount : la route journalise ce compte réel, jamais la taille de la
        sélection demandée.
        """
        if not listing_ids:
            return 0
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM listings WHERE listing_id = ANY(%s)",
                    (list(listing_ids),),
                )
                deleted = cur.rowcount
                conn.commit()
                return deleted
        finally:
            self._release_conn(conn)

    def get_listing_detail(self, listing_id: str) -> dict | None:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
                listing = cur.fetchone()
                if not listing:
                    return None
                result = dict(listing)

                cur.execute(
                    """SELECT s.id, s.label, s.source, u.username
                       FROM search_listings sl
                       JOIN searches s ON s.id = sl.search_id
                       JOIN users u ON u.id = s.user_id
                       WHERE sl.listing_id = %s""",
                    (listing_id,),
                )
                result["linked_searches"] = [dict(r) for r in cur.fetchall()]
                return result
        finally:
            self._release_conn(conn)

    def get_unique_agencies_for_user(self, user_id: int) -> list[str]:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT l.agency
                       FROM listings l
                       JOIN search_listings sl ON sl.listing_id = l.listing_id
                       JOIN searches s ON s.id = sl.search_id
                       WHERE s.user_id = %s AND l.agency IS NOT NULL AND l.agency != ''
                       ORDER BY l.agency""",
                    (user_id,),
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            self._release_conn(conn)
