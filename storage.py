"""PostgreSQL storage for multi-user listing tracking.

Owns the DB connections and schema, and exposes the repositories:
  - users (UserRepository)
  - searches (SearchRepository)
  - listings (ListingRepository)
  - scrape_logs (ScrapeLogRepository)
  - admin (AdminRepository)
  - settings (SettingsRepository)

Call methods on the relevant repository directly, e.g. storage.searches.get_search(id).
"""

from __future__ import annotations

from loguru import logger

# Issue #12 : le contrat de normalisation de creation_date (date de
# publication SOURCE, ISO-8601 UTC ou sentinelle « unknown ») est défini
# UNE seule fois dans parsers/_dates.py — les parsers l'appliquent à
# l'écriture, la migration ci-dessous à la reprise des valeurs héritées.
# Aucune duplication volontaire : toute évolution du parsing se fait là-bas.
from parsers._dates import normaliser_creation_date  # noqa: E402
from repositories.admin_repo import AdminRepository
from repositories.bienici_geo_repo import BienIciGeoRepository
from repositories.century21_geo_repo import Century21GeoRepository
from repositories.citya_geo_repo import CityaGeoRepository
from repositories.commune_geo_repo import CommuneGeoRepository
from repositories.foncia_geo_repo import FonciaGeoRepository
from repositories.guyhoquet_geo_repo import GuyHoquetGeoRepository
from repositories.listing_repo import ListingRepository
from repositories.map_pin_repo import MapPinRepository
from repositories.orpi_geo_repo import OrpiGeoRepository
from repositories.pap_geo_repo import PapGeoRepository
from repositories.scrape_log_repo import ScrapeLogRepository
from repositories.search_repo import SearchRepository
from repositories.seloger_geo_repo import SelogerGeoRepository
from repositories.settings_repo import SettingsRepository
from repositories.user_repo import UserRepository


class Storage:
    """Owns DB connections/schema and gives access to the repositories."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.users = UserRepository(database_url)
        self.searches = SearchRepository(database_url)
        self.listings = ListingRepository(database_url)
        self.scrape_logs = ScrapeLogRepository(database_url)
        self.admin = AdminRepository(database_url)
        self.settings = SettingsRepository(database_url)
        self.seloger_geo = SelogerGeoRepository(database_url)
        self.bienici_geo = BienIciGeoRepository(database_url)
        self.century21_geo = Century21GeoRepository(database_url)
        self.pap_geo = PapGeoRepository(database_url)
        self.orpi_geo = OrpiGeoRepository(database_url)
        self.foncia_geo = FonciaGeoRepository(database_url)
        self.guyhoquet_geo = GuyHoquetGeoRepository(database_url)
        self.citya_geo = CityaGeoRepository(database_url)
        # Issue #26 : cache des centres de communes (fallback géocodage
        # geo.api.gouv.fr) et repères personnels des utilisateurs.
        self.commune_geo = CommuneGeoRepository(database_url)
        self.map_pins = MapPinRepository(database_url)
        self._init_db()

    @classmethod
    def run_migrations(cls, database_url: str) -> None:
        """Apply the DDL migrations against database_url.

        Safe to run against a fresh OR an already-populated database: every
        statement is idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN ...
        IF NOT EXISTS). Bypasses __init__'s _init_db() check (which requires
        tables to already exist) since this IS how they get created/updated.
        Used by scripts/migrate.py — run this after every deploy that
        changes the schema.
        """
        instance = cls.__new__(cls)
        instance.database_url = database_url
        instance.users = UserRepository(database_url)
        instance._run_ddl_migrations()

    # ------------------------------------------------------------------
    # Connection management (shared by all repos via inheritance)
    # ------------------------------------------------------------------

    def _get_conn(self):
        return self.users._get_conn()

    def _get_conn_for_request(self):
        return self.users._get_conn_for_request()

    def _release_conn(self, conn):
        return self.users._release_conn(conn)

    def release_to_pool(self, conn):
        return self.users.release_to_pool(conn)

    def _get_ddl_conn(self):
        return self.users._get_ddl_conn()

    def _close_conn(self, conn):
        return self.users._close_conn(conn)

    def _init_db(self) -> None:
        """Verify tables exist. Never does DDL at runtime."""
        import time
        total_start = time.monotonic()

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'users'
                    )
                """)
                tables_exist = cur.fetchone()[0]
            if not tables_exist:
                logger.error("Tables DB manquantes — exécutez les migrations manuellement")
                raise RuntimeError("Database tables not found. Run migrations first.")
        finally:
            self._release_conn(conn)

        elapsed = time.monotonic() - total_start
        logger.debug(f"DB init check: {elapsed:.3f}s (tables OK)")

    def _run_ddl_migrations(self) -> None:
        """Run DDL migrations — execute ONCE at first deployment."""
        import time
        total_start = time.monotonic()
        phase_start = total_start

        def _log_phase(name: str):
            elapsed = time.monotonic() - phase_start
            logger.debug(f"  DB phase '{name}': {elapsed:.2f}s")
            return time.monotonic()

        conn = self._get_ddl_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        username    TEXT UNIQUE NOT NULL,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS searches (
                        id              SERIAL PRIMARY KEY,
                        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        label           TEXT NOT NULL,
                        ntfy_topic      TEXT NOT NULL,
                        source          TEXT NOT NULL DEFAULT 'seloger',
                        criteria        JSONB DEFAULT '{}',
                        scrape_interval INTEGER DEFAULT 5,
                        last_scraped    TIMESTAMP,
                        is_active       BOOLEAN DEFAULT TRUE,
                        -- Issue #10 : les notifications ntfy sont activées par
                        -- défaut ; désactivables recherche par recherche sans
                        -- suspendre le scraping.
                        notify_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS listings (
                        listing_id      TEXT PRIMARY KEY,
                        url             TEXT NOT NULL,
                        title           TEXT,
                        price           TEXT,
                        surface         TEXT,
                        rooms           TEXT,
                        location        TEXT,
                        image_url       TEXT,
                        description     TEXT,
                        agency          TEXT,
                        source          TEXT DEFAULT '',
                        legacy_id       TEXT DEFAULT '',
                        price_value     FLOAT,
                        price_details   TEXT DEFAULT '',
                        city            TEXT DEFAULT '',
                        district        TEXT DEFAULT '',
                        zip_code        TEXT DEFAULT '',
                        property_type   TEXT DEFAULT '',
                        is_private      BOOLEAN DEFAULT FALSE,
                        phone           JSONB DEFAULT '[]',
                        epc             TEXT DEFAULT '',
                        ges             TEXT DEFAULT '',
                        is_new          BOOLEAN DEFAULT FALSE,
                        is_exclusive    BOOLEAN DEFAULT FALSE,
                        has_3d_visit    BOOLEAN DEFAULT FALSE,
                        -- Issue #12 : « unknown » plutôt que chaîne vide
                        -- (sentinelle explicite, tri cohérent).
                        creation_date   TEXT DEFAULT 'unknown',
                        update_date     TEXT DEFAULT '',
                        headline        TEXT DEFAULT '',
                        photos          JSONB DEFAULT '[]',
                        first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS search_listings (
                        search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                        listing_id  TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
                        found_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notified    BOOLEAN DEFAULT TRUE,
                        PRIMARY KEY (search_id, listing_id)
                    );
                """)
                # Issue #26 : repères personnels GLOBAUX d'un utilisateur
                # (pas liés à une recherche). Les CHECK excluent (0, 0) — le
                # « golfe de Guinée » des coords manquantes mal filtrées — et
                # toute valeur hors bornes géographiques : un pin invalide ne
                # doit jamais pouvoir exister en base.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS map_pins (
                        id          SERIAL PRIMARY KEY,
                        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        label       TEXT NOT NULL,
                        note        TEXT DEFAULT '',
                        icon        TEXT DEFAULT '📍',
                        latitude    DOUBLE PRECISION NOT NULL
                                    CHECK (latitude <> 0 AND latitude BETWEEN -90 AND 90),
                        longitude   DOUBLE PRECISION NOT NULL
                                    CHECK (longitude <> 0 AND longitude BETWEEN -180 AND 180),
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS admin_logs (
                        id            SERIAL PRIMARY KEY,
                        action        TEXT NOT NULL,
                        details       TEXT,
                        performed_by  TEXT,
                        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
                # L'API BFF SeLoger a été retirée (elle renvoie 400 depuis
                # que son schéma a changé — voir scraper/seloger.py) : le
                # réglage qui permettait de l'activer/désactiver n'a plus
                # d'objet. Supprimé ici pour que les bases déjà déployées ne
                # gardent pas un réglage mort.
                cur.execute("DELETE FROM app_settings WHERE key = 'use_bff_api';")
                # `area_key` identifie un périmètre à n'importe quel niveau —
                # un code INSEE de commune, "city:<insee>", "dept:<code>" ou
                # "region:<code>" (voir services.seloger_geocode.area_cache_key).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS seloger_place_ids (
                        area_key    TEXT PRIMARY KEY,
                        place_id    TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # La colonne s'appelait insee_code quand seules les communes
                # étaient gérées : renommage idempotent pour les bases déjà
                # créées avec l'ancien nom (les codes INSEE nus restent des
                # clés valides, rien à réécrire).
                cur.execute("""
                    DO $$ BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'seloger_place_ids'
                              AND column_name = 'insee_code'
                        ) THEN
                            ALTER TABLE seloger_place_ids RENAME COLUMN insee_code TO area_key;
                        END IF;
                    END $$;
                """)
                # Même clé de périmètre (`area_key`) que seloger_place_ids —
                # voir services.bienici_geocode.area_cache_key. `zone_ids`
                # est un tableau JSON (contrairement au place_id unique de
                # SeLoger, un périmètre bienici peut avoir plusieurs zoneIds,
                # ex. une région = union de ses départements).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bienici_zone_ids (
                        area_key    TEXT PRIMARY KEY,
                        zone_ids    TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les caches SeLoger et
                # bienici — voir services.century21_geocode.area_cache_key.
                # `slug_id` est le slug d'URL Century 21 (v-paris, cp-75001),
                # une valeur unique comme le place_id de SeLoger.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS century21_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        slug_id     TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les caches SeLoger,
                # bienici et century21 — voir services.pap_geocode.area_cache_key.
                # `geo_id` est l'identifiant numérique opaque de pap.fr (« 439 »
                # pour Paris), stocké en TEXT comme le slug_id de Century 21 :
                # une valeur unique par périmètre.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pap_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        geo_id      TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les caches SeLoger,
                # bienici, century21 et pap — voir services.orpi_geocode.
                # area_cache_key. `slug_id` est le slug d'URL Orpi renvoyé par
                # son autocomplete (`rosny-sous-bois`, `gironde`), une valeur
                # unique comme le slug_id de Century 21.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orpi_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        slug_id     TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les autres caches
                # géo — voir services.foncia_geocode.area_cache_key.
                # `slug_id` est le slug de localité Foncia renvoyé par son
                # API géo (`vannes-56000`, `haute-garonne-31`, `occitanie`),
                # une valeur unique comme le slug_id d'Orpi.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS foncia_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        slug_id     TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les autres caches
                # géo — voir services.guyhoquet_geocode.area_cache_key.
                # `slug_id` est l'identifiant de localisation Guy Hoquet
                # renvoyé par son autocomplete (`toulouse-31000_c3`,
                # `lyon-69123_c4`), une valeur unique comme le slug_id de
                # Century 21. Départements et régions ne sont jamais cachés :
                # leur slug se dérive du code sans réseau.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS guyhoquet_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        slug_id     TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Même clé de périmètre (`area_key`) que les autres caches
                # géo — voir services.citya_geocode.area_cache_key.
                # `slug_id` est le slug de recherche Citya renvoyé par son
                # autocomplete (`toulouse-31555`, `lyon-69000`,
                # `haute-garonne-31`), une valeur unique comme le slug_id de
                # Foncia. Aucun niveau ne se dérive localement : le site
                # replie silencieusement sur la France entière quand un slug
                # ne colle pas, tout périmètre résolu passe donc par ce cache.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS citya_geo_ids (
                        area_key    TEXT PRIMARY KEY,
                        slug_id     TEXT,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Issue #26 : centre géographique d'une commune, résolu par le
                # fallback geo.api.gouv.fr — voir services.geocode_commune.
                # Même pattern que les caches géo ci-dessus (`area_key` en clé,
                # ici « postal:<cp> » ou code INSEE nu). Contrairement à eux,
                # seuls les SUCCÈS sont mémorisés : un CP non résolu est
                # re-tenté au scrape suivant plutôt que gelé 7 jours.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS commune_centres (
                        area_key    TEXT PRIMARY KEY,
                        latitude    DOUBLE PRECISION,
                        longitude   DOUBLE PRECISION,
                        resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                phase_start = _log_phase("create_tables")

                conn.commit()

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listings_first_seen
                        ON listings(first_seen);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_search
                        ON search_listings(search_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_found_at
                        ON search_listings(found_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_search_found
                        ON search_listings(search_id, found_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_listing
                        ON search_listings(listing_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_user
                        ON searches(user_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_user_active
                        ON searches(user_id, is_active);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_source
                        ON searches(source);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_source
                ON listings(source);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_price_value
                ON listings(price_value);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_property_type
                ON listings(property_type);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_city
                ON listings(city);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_listings_creation_date
                ON listings(creation_date);
                """)
                cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_logs_created
                ON admin_logs(created_at);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_admin_logs_action
                        ON admin_logs(action);
                """)
                # Issue #26 : la lecture carte ne parcourt que les annonces
                # géolocalisées — l'index partiel épouse exactement ce filtre.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listings_coords
                        ON listings(latitude, longitude)
                        WHERE latitude IS NOT NULL AND longitude IS NOT NULL;
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_map_pins_user
                        ON map_pins(user_id);
                """)
                phase_start = _log_phase("indexes")

                cur.execute("""
                    ALTER TABLE listings
                        ADD COLUMN IF NOT EXISTS legacy_id TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS price_value FLOAT,
                        ADD COLUMN IF NOT EXISTS price_details TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS city TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS district TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS zip_code TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS property_type TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS is_private BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS phone JSONB DEFAULT '[]',
                        ADD COLUMN IF NOT EXISTS epc TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS ges TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS is_new BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS is_exclusive BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS has_3d_visit BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS creation_date TEXT DEFAULT 'unknown',
                        ADD COLUMN IF NOT EXISTS update_date TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS headline TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS photos JSONB DEFAULT '[]',
                        -- Issue #26 : géolocalisation hybride — extraction
                        -- native par source (bienici/ORPI/Foncia/essetpm),
                        -- sinon fallback centre de commune au scrape. Les
                        -- trois colonnes restent NULLables : une annonce sans
                        -- coordonnées reste valide, simplement absente de la
                        -- carte. `location_precision` vaut 'exacte',
                        -- 'approximative' ou 'commune'.
                        ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS location_precision TEXT;
                """)
                phase_start = _log_phase("alter_listings")

                cur.execute("""
                    ALTER TABLE searches
                        ADD COLUMN IF NOT EXISTS criteria JSONB DEFAULT '{}',
                        ADD COLUMN IF NOT EXISTS scrape_interval INTEGER DEFAULT 5,
                        ADD COLUMN IF NOT EXISTS last_scraped TIMESTAMP,
                        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
                        ADD COLUMN IF NOT EXISTS blacklisted_agencies TEXT[] DEFAULT '{}',
                        ADD COLUMN IF NOT EXISTS blacklist_mode TEXT DEFAULT 'exclude',
                        -- Issue #10 : NOT NULL + DEFAULT TRUE backfill les
                        -- recherches existantes à TRUE (rétrocompatible, pas
                        -- de réécriture de table pour une constante).
                        ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        ADD COLUMN IF NOT EXISTS sources JSONB DEFAULT NULL;
                """)
                # Backfill : une recherche créée avant l'ajout du multi-source
                # n'a que `source` — on la reflète dans `sources` pour que le
                # pipeline de scraping (qui lit `sources`) la traite pareil.
                cur.execute("""
                    UPDATE searches SET sources = to_jsonb(ARRAY[source])
                    WHERE sources IS NULL;
                """)
                cur.execute("""
                    ALTER TABLE search_listings
                        ADD COLUMN IF NOT EXISTS notified BOOLEAN DEFAULT TRUE;
                """)
                # Le token API (X-API-Token) a été supprimé (issue #30) :
                # plus d'API pilotée par script, l'authentification passe par
                # les sessions Flask. La colonne devient morte ; les entrées
                # d'audit « user_token_reset » historiques restent en base
                # (pas de migration de données).
                cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS api_token;")
                # Issue #12 : fiabilisation de creation_date (date de
                # publication SOURCE, pas la récupération scraper). La
                # colonne reste en TEXT mais ne contient plus que deux
                # formes : l'ISO-8601 UTC canonique
                # (« YYYY-MM-DDTHH:MM:SS+00:00 ») ou la sentinelle
                # « unknown ». Vides/NULL → « unknown » ; formats
                # historiques hétérogènes (SeLoger « YYYY-MM-DD », bienici
                # ISO+Z, orpi/foncia ISO+02:00, guyhoquet naïf) normalisés
                # en Python par normaliser_creation_date() ; le garbage non
                # parsable devient « unknown » plutôt que de faire échouer
                # la migration. Idempotent : les valeurs déjà canoniques ne
                # sont pas réécrites.
                cur.execute("""
                    UPDATE listings SET creation_date = 'unknown'
                    WHERE creation_date IS NULL OR creation_date = '';
                """)
                cur.execute("""
                    ALTER TABLE listings
                        ALTER COLUMN creation_date SET DEFAULT 'unknown';
                """)
                cur.execute("""
                    SELECT DISTINCT creation_date FROM listings
                    WHERE creation_date <> 'unknown';
                """)
                for (brute,) in cur.fetchall():
                    canonique = normaliser_creation_date(brute)
                    if canonique != brute:
                        cur.execute(
                            "UPDATE listings SET creation_date = %s WHERE creation_date = %s;",
                            (canonique, brute),
                        )
                phase_start = _log_phase("alter_other")

                conn.commit()

            total_elapsed = time.monotonic() - total_start
            logger.info(f"Tables PostgreSQL initialisées en {total_elapsed:.2f}s")
        finally:
            # Connexion DDL brute (hors pool) : toujours fermée directement.
            self._close_conn(conn)

