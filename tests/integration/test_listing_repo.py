"""ListingRepository contre un vrai Postgres.

Les constructeurs de clauses (`_build_filter_clauses`, `_build_order_clause`)
sont couverts en unitaire — voir tests/unit/test_listing_sql_builders.py, qui
prouve qu'aucune valeur utilisateur n'atterrit dans la chaîne SQL. Ici on prouve
la moitié complémentaire, celle qu'aucun double ne peut donner :

1. **Le SQL généré s'exécute vraiment.** `execute_values`, `ON CONFLICT DO
   NOTHING` + `RETURNING`, `= ANY(%s)`, `make_interval(days => %s)`.
2. **Les CAST sur colonnes TEXT.** `surface` et `rooms` sont des `TEXT`
   (« 65 m² », « 3 pièces ») : filtrer et trier dessus passe par
   `CAST(NULLIF(REGEXP_REPLACE(...), '') AS NUMERIC)`. Ce que ce nettoyage fait
   de « 65,5 m² » ou de « abc » ne se déduit d'aucun test unitaire — et deux des
   cas trouvés ici sont des bugs (voir `TestNumericFiltersOnTextColumns`).
3. **La parité liste/comptage.** Le test unitaire compare les deux `WHERE`
   *textuellement* ; ici on compare les deux *résultats* sur de vraies lignes.

`found_at` et `first_seen` sont remplis par `CURRENT_TIMESTAMP` côté serveur, et
toutes les lignes d'un même `save_and_link` partagent donc la même valeur : les
tests qui portent sur l'ordre chronologique vieillissent les lignes en SQL brut.
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.helpers.factories import make_listing
from tests.integration.conftest import insert_search

# ---------------------------------------------------------------------------
# save_and_link
# ---------------------------------------------------------------------------

class TestSaveAndLink:
    def test_a_new_listing_is_stored_linked_and_left_unnotified(self, storage, search, sql):
        """`notified` a un `DEFAULT TRUE` en base (hérité du backfill qui a
        marqué l'existant comme déjà envoyé) : le repo doit passer `FALSE`
        explicitement, sinon aucune notification ne partirait jamais — et sans
        aucune erreur."""
        new_for_search, already_linked = storage.listings.save_and_link(
            [make_listing(listing_id="sl_1", title="Studio")], search["id"],
        )

        assert [item.listing_id for item in new_for_search] == ["sl_1"]
        assert already_linked == []
        assert sql.row(
            "SELECT listing_id, title FROM listings", (),
        ) == ("sl_1", "Studio")
        assert sql.row(
            "SELECT listing_id, notified FROM search_listings WHERE search_id = %s", (search["id"],),
        ) == ("sl_1", False)

    def test_an_empty_batch_is_a_no_op(self, storage, search, sql):
        assert storage.listings.save_and_link([], search["id"]) == ([], [])

        assert sql.one("SELECT COUNT(*) FROM listings") == 0

    def test_relinking_the_same_listing_returns_it_as_already_known(self, storage, search, sql):
        """Le `RETURNING listing_id` du second appel ne rend rien (la clé
        primaire `(search_id, listing_id)` existe déjà) : c'est Postgres, et non
        un calcul Python, qui décide qu'il n'y a rien de neuf à notifier."""
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], search["id"])

        new_for_search, already_linked = storage.listings.save_and_link(
            [make_listing(listing_id="sl_1")], search["id"],
        )

        assert new_for_search == []
        assert [item.listing_id for item in already_linked] == ["sl_1"]
        assert sql.one("SELECT COUNT(*) FROM search_listings") == 1
        assert sql.one("SELECT COUNT(*) FROM listings") == 1

    def test_a_second_scrape_splits_new_from_already_known(self, storage, search):
        storage.listings.save_and_link(
            [make_listing(listing_id="vue"), make_listing(listing_id="vue_aussi")], search["id"],
        )

        new_for_search, already_linked = storage.listings.save_and_link(
            [
                make_listing(listing_id="vue"),
                make_listing(listing_id="neuve"),
                make_listing(listing_id="vue_aussi"),
            ],
            search["id"],
        )

        assert [item.listing_id for item in new_for_search] == ["neuve"]
        assert {item.listing_id for item in already_linked} == {"vue", "vue_aussi"}

    def test_an_existing_listing_is_not_overwritten_by_a_later_scrape(self, storage, search, sql):
        """`ON CONFLICT (listing_id) DO NOTHING` : le prix et le titre du premier
        passage restent en base. C'est ce qui rend le scrape idempotent, mais
        c'est aussi pourquoi une baisse de prix n'est jamais reflétée —
        comportement voulu et figé ici."""
        storage.listings.save_and_link(
            [make_listing(listing_id="sl_1", title="Avant", price_value=1000.0)], search["id"],
        )

        storage.listings.save_and_link(
            [make_listing(listing_id="sl_1", title="Après", price_value=2000.0)], search["id"],
        )

        assert sql.row("SELECT title, price_value FROM listings") == ("Avant", 1000.0)

    def test_a_batch_larger_than_the_page_size_is_fully_inserted(self, storage, search, sql):
        """`page_size=100` découpe l'envoi en plusieurs requêtes : avec 250
        annonces il y en a trois, et le `fetch=True` du second `execute_values`
        doit agréger les `RETURNING` des trois pages. Une pagination mal recollée
        ne perdrait pas de lignes en base — elle perdrait des *notifications*."""
        listings = [make_listing(listing_id=f"sl_{i:03d}") for i in range(250)]

        new_for_search, already_linked = storage.listings.save_and_link(listings, search["id"])

        assert len(new_for_search) == 250
        assert already_linked == []
        assert sql.one("SELECT COUNT(*) FROM listings") == 250
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE notified = FALSE") == 250

    def test_a_large_batch_partially_already_known_is_split_correctly(self, storage, search):
        """Le cas réel d'un gros scrape : la majorité des annonces est déjà
        connue, et le découpage en pages ne doit pas brouiller la partition."""
        first = [make_listing(listing_id=f"sl_{i:03d}") for i in range(150)]
        storage.listings.save_and_link(first, search["id"])

        second = [make_listing(listing_id=f"sl_{i:03d}") for i in range(120, 270)]
        new_for_search, already_linked = storage.listings.save_and_link(second, search["id"])

        assert {item.listing_id for item in new_for_search} == {f"sl_{i:03d}" for i in range(150, 270)}
        assert {item.listing_id for item in already_linked} == {f"sl_{i:03d}" for i in range(120, 150)}

    def test_a_lone_surrogate_survives_the_round_trip_to_postgres(self, storage, search, sql):
        """🔒 C'est *ici* que ça casse sans `_clean_string`, pas en unitaire : le
        surrogate est un `str` Python parfaitement valide, mais psycopg2 doit
        l'encoder en UTF-8 pour l'envoyer, et lève `UnicodeEncodeError` — perdant
        tout le lot d'annonces, pas seulement la fautive.
        """
        listing = make_listing(
            listing_id="sl_surrogate",
            title="Studio \ud800 Paris",
            agency="Agence \udfff",
            description="désc \udcff ription",
            location="Paris \ud800",
        )

        new_for_search, _ = storage.listings.save_and_link([listing], search["id"])

        assert [item.listing_id for item in new_for_search] == ["sl_surrogate"]
        title, agency = sql.row("SELECT title, agency FROM listings")
        # Le double aller-retour `surrogatepass` → `replace` fait passer le
        # surrogate par ses 3 octets, chacun remplacé par U+FFFD : la donnée est
        # abîmée, mais le lot est sauvé. C'est le compromis assumé.
        assert "\ud800" not in title
        assert title.startswith("Studio ") and title.endswith(" Paris")
        assert "�" in title
        assert agency.startswith("Agence ") and "\udfff" not in agency
        assert title.encode("utf-8")

    def test_a_surrogate_in_a_batch_does_not_take_the_batch_down(self, storage, search, sql):
        """Le lot est tout-ou-rien (une transaction) : une seule annonce mal
        encodée ferait perdre les 99 autres."""
        listings = [make_listing(listing_id=f"ok_{i}") for i in range(5)]
        listings.insert(2, make_listing(listing_id="sale", title="Titre \ud800"))

        storage.listings.save_and_link(listings, search["id"])

        assert sql.one("SELECT COUNT(*) FROM listings") == 6

    def test_two_searches_linking_the_same_listing_keep_independent_notified_states(
        self, storage, user, sql,
    ):
        """L'annonce est stockée une fois, le *lien* deux fois. Notifier pour
        l'une ne doit pas consommer la notification de l'autre — sinon un
        utilisateur ne reçoit jamais une annonce déjà envoyée à quelqu'un
        d'autre."""
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        storage.listings.save_and_link([make_listing(listing_id="partagee")], first["id"])
        new_for_second, _ = storage.listings.save_and_link(
            [make_listing(listing_id="partagee")], second["id"],
        )

        assert [item.listing_id for item in new_for_second] == ["partagee"]
        storage.listings.mark_listings_notified(first["id"], ["partagee"])

        assert storage.listings.get_unnotified_listings_for_search(first["id"]) == []
        assert [
            item.listing_id for item in storage.listings.get_unnotified_listings_for_search(second["id"])
        ] == ["partagee"]
        assert sql.one("SELECT COUNT(*) FROM listings") == 1
        assert sql.one("SELECT COUNT(*) FROM search_listings") == 2

    def test_a_duplicate_inside_one_batch_is_reported_as_new_twice(self, storage, search, sql):
        """# BUG : une annonce présente DEUX FOIS dans le même lot (chevauchement
        de pagination côté source, cas courant) n'est liée qu'une fois en base —
        `ON CONFLICT DO NOTHING` fait son travail — mais la partition est
        recalculée en Python par appartenance d'identifiant :

            new_for_search = [item for item in listings if item.listing_id in newly_linked_ids]

        Les DEUX objets passent le test, donc `ScrapeService` envoie deux
        notifications pour la même annonce. Le compteur « n nouvelles annonces »
        est faux du même coup. Comportement ACTUEL figé ici.
        """
        doublon = make_listing(listing_id="sl_1")

        new_for_search, already_linked = storage.listings.save_and_link(
            [doublon, make_listing(listing_id="sl_1")], search["id"],
        )

        assert [item.listing_id for item in new_for_search] == ["sl_1", "sl_1"]
        assert already_linked == []
        # La base, elle, est correcte : une annonce, un lien.
        assert sql.one("SELECT COUNT(*) FROM listings") == 1
        assert sql.one("SELECT COUNT(*) FROM search_listings") == 1

    def test_an_unknown_search_id_violates_the_foreign_key_and_rolls_back(self, storage, sql):
        """`search_listings.search_id` référence `searches` : lier à une
        recherche supprimée entre-temps (le scrape tourne sur un thread de fond)
        lève. Le rollback doit alors aussi annuler l'insertion des annonces,
        sinon elles resteraient orphelines en base."""
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            storage.listings.save_and_link([make_listing(listing_id="sl_1")], 999_999)

        assert sql.one("SELECT COUNT(*) FROM listings") == 0

    def test_the_connection_is_usable_after_that_rollback(self, storage, search):
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            storage.listings.save_and_link([make_listing(listing_id="sl_1")], 999_999)

        new_for_search, _ = storage.listings.save_and_link(
            [make_listing(listing_id="sl_2")], search["id"],
        )
        assert [item.listing_id for item in new_for_search] == ["sl_2"]


# ---------------------------------------------------------------------------
# link_listing_to_search
# ---------------------------------------------------------------------------

class TestLinkListingToSearch:
    def test_linking_an_existing_listing_returns_true(self, storage, user, sql):
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], first["id"])

        assert storage.listings.link_listing_to_search(second["id"], "sl_1") is True

        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE search_id = %s", (second["id"],)) == 1

    def test_the_default_true_applies_when_notified_is_not_specified(self, storage, user, sql):
        """# Piège : contrairement à `save_and_link`, cette méthode n'écrit pas
        `notified`, donc le `DEFAULT TRUE` du schéma s'applique. Une annonce liée
        par ce chemin est considérée comme DÉJÀ notifiée et ne sera jamais
        envoyée. Aucun appelant de production ne l'utilise aujourd'hui — le test
        fige l'écart pour qu'un futur usage ne découvre pas le silence en prod.
        """
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], first["id"])

        storage.listings.link_listing_to_search(second["id"], "sl_1")

        assert sql.one(
            "SELECT notified FROM search_listings WHERE search_id = %s", (second["id"],),
        ) is True
        assert storage.listings.get_unnotified_listings_for_search(second["id"]) == []

    def test_a_duplicate_link_returns_false_instead_of_raising(self, storage, search):
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], search["id"])

        assert storage.listings.link_listing_to_search(search["id"], "sl_1") is False

    def test_an_unknown_listing_id_returns_false(self, storage, search):
        """`ForeignKeyViolation` dérive d'`IntegrityError` : le même `except` le
        rattrape, donc le repo ne distingue pas « déjà lié » de « annonce
        inexistante ». Les deux rendent False."""
        assert storage.listings.link_listing_to_search(search["id"], "jamais_vue") is False

    @pytest.mark.parametrize(
        ("search_id", "listing_id"),
        [
            pytest.param(None, "sl_1", id="doublon"),
            pytest.param(None, "inexistante", id="cle-etrangere"),
        ],
    )
    def test_the_connection_survives_the_integrity_error(self, storage, search, search_id, listing_id):
        """🔒 Le point sensible : l'`IntegrityError` avorte la transaction. Sans
        le `conn.rollback()` du bloc `except`, la connexion repartirait au pool
        en transaction avortée et la requête HTTP suivante répondrait « current
        transaction is aborted ». On enchaîne donc de vraies opérations juste
        après l'échec."""
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], search["id"])

        assert storage.listings.link_listing_to_search(search["id"], listing_id) is False

        assert storage.listings.count_listings_for_search(search["id"]) == 1
        assert storage.listings.get_listing_detail("sl_1")["listing_id"] == "sl_1"
        new_for_search, _ = storage.listings.save_and_link(
            [make_listing(listing_id="sl_2")], search["id"],
        )
        assert [item.listing_id for item in new_for_search] == ["sl_2"]


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class TestNotificationState:
    def test_unnotified_listings_are_returned_as_listing_objects(self, storage, search):
        storage.listings.save_and_link(
            [make_listing(listing_id="sl_1", title="Studio", price_value=990.0)], search["id"],
        )

        (listing,) = storage.listings.get_unnotified_listings_for_search(search["id"])

        assert listing.listing_id == "sl_1"
        assert listing.title == "Studio"
        assert listing.price_value == 990.0
        # `Listing.from_dict` écarte les colonnes qui ne sont pas des champs du
        # dataclass (`first_seen`), sinon la construction lèverait.
        assert not hasattr(listing, "first_seen")

    def test_mark_notified_uses_a_single_array_parameter(self, storage, search, sql):
        """`= ANY(%s)` : psycopg2 adapte la liste Python en tableau Postgres, une
        seule liaison de paramètre quel que soit le nombre d'annonces. Un `IN
        (...)` construit à la main casserait au-delà de la limite de
        paramètres."""
        storage.listings.save_and_link(
            [make_listing(listing_id=f"sl_{i}") for i in range(5)], search["id"],
        )

        storage.listings.mark_listings_notified(search["id"], ["sl_0", "sl_2", "sl_4"])

        assert {
            item.listing_id for item in storage.listings.get_unnotified_listings_for_search(search["id"])
        } == {"sl_1", "sl_3"}
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE notified") == 3

    def test_marking_a_large_array_works_in_one_statement(self, storage, search):
        listings = [make_listing(listing_id=f"sl_{i:03d}") for i in range(250)]
        storage.listings.save_and_link(listings, search["id"])

        storage.listings.mark_listings_notified(search["id"], [item.listing_id for item in listings])

        assert storage.listings.get_unnotified_listings_for_search(search["id"]) == []

    def test_marking_ids_that_are_not_linked_changes_nothing(self, storage, search):
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], search["id"])

        storage.listings.mark_listings_notified(search["id"], ["inconnue", "autre"])

        assert len(storage.listings.get_unnotified_listings_for_search(search["id"])) == 1

    def test_an_empty_list_of_ids_is_a_no_op(self, storage, search):
        storage.listings.save_and_link([make_listing(listing_id="sl_1")], search["id"])

        assert storage.listings.mark_listings_notified(search["id"], []) is None

        assert len(storage.listings.get_unnotified_listings_for_search(search["id"])) == 1

    def test_the_update_never_crosses_over_to_another_search(self, storage, user, other_search):
        """Un `listing_id` est global : sans le `search_id` dans le `WHERE`,
        notifier pour Alice marquerait l'annonce notifiée pour Bob, qui ne la
        recevrait jamais."""
        mine = insert_search(storage, user["id"], "À moi")
        storage.listings.save_and_link([make_listing(listing_id="partagee")], mine["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], other_search["id"])

        storage.listings.mark_listings_notified(mine["id"], ["partagee"])

        assert len(storage.listings.get_unnotified_listings_for_search(other_search["id"])) == 1

    def test_unnotified_of_a_search_without_listings_is_empty(self, storage, search):
        assert storage.listings.get_unnotified_listings_for_search(search["id"]) == []
        assert storage.listings.get_unnotified_listings_for_search(999_999) == []


# ---------------------------------------------------------------------------
# Filtres numériques sur colonnes TEXT — invérifiable sans moteur
# ---------------------------------------------------------------------------

@pytest.fixture
def text_surface_dataset(storage, search, sql):
    """Un jeu de `surface`/`rooms` tel qu'un parser en produit réellement.

    Les valeurs vides et NULL sont posées en SQL brut : le dataclass `Listing`
    a `""` par défaut et `save_and_link` ne sait pas écrire NULL sans qu'on l'y
    force.
    """
    storage.listings.save_and_link(
        [
            make_listing(listing_id="propre", surface="65", rooms="3"),
            make_listing(listing_id="unite", surface="80 m²", rooms="4 pièces"),
            make_listing(listing_id="decimal", surface="12.5 m²", rooms="1"),
            make_listing(listing_id="virgule", surface="65,5 m²", rooms="2"),
            make_listing(listing_id="vide", surface="", rooms=""),
            make_listing(listing_id="texte", surface="non communiquée", rooms="studio"),
        ],
        search["id"],
    )
    sql.exec("UPDATE listings SET surface = NULL, rooms = NULL WHERE listing_id = 'propre'")
    sql.exec(
        "INSERT INTO listings (listing_id, url, surface, rooms) VALUES ('nul', 'http://x', NULL, NULL)",
    )
    sql.exec(
        "INSERT INTO search_listings (search_id, listing_id, notified) VALUES (%s, 'nul', FALSE)",
        (search["id"],),
    )
    return search


class TestNumericFiltersOnTextColumns:
    """`surface` et `rooms` sont des colonnes TEXT filtrées numériquement via
    `CAST(NULLIF(REGEXP_REPLACE(col, '[^0-9.]', '', 'g'), '') AS NUMERIC)`.
    Ce que ce nettoyage rend pour chaque forme réelle n'est vérifiable que
    contre le moteur : la regex, le `NULLIF` et le `CAST` interagissent.
    """

    def _ids(self, storage, search_id, **filters):
        return {
            row["listing_id"]
            for row in storage.listings.get_listings_for_search(search_id, limit=100, filters=filters)
        }

    def test_a_unit_suffix_is_stripped_and_the_value_compares_numerically(self, storage, text_surface_dataset):
        """« 80 m² » se compare bien à 80, et « 4 pièces » à 4 : c'est tout
        l'objet du `REGEXP_REPLACE`."""
        search_id = text_surface_dataset["id"]

        assert "unite" in self._ids(storage, search_id, surface_min=80, surface_max=80)
        assert "unite" in self._ids(storage, search_id, rooms_min=4, rooms_max=4)

    def test_a_decimal_point_survives_the_cleanup(self, storage, text_surface_dataset):
        """Le `.` est dans la classe conservée : « 12.5 m² » vaut 12,5 et tombe
        donc en dehors d'un filtre `surface_min=13`."""
        search_id = text_surface_dataset["id"]

        assert "decimal" in self._ids(storage, search_id, surface_min=12, surface_max=13)
        assert "decimal" not in self._ids(storage, search_id, surface_min=13)

    def test_a_french_decimal_comma_multiplies_the_surface_by_ten(self, storage, text_surface_dataset):
        """# BUG : la virgule décimale française n'est pas dans la classe
        conservée (`[^0-9.]` la supprime au lieu de la convertir en point).
        « 65,5 m² » devient donc la chaîne « 655 », soit 655 m².

        Conséquences concrètes : l'annonce disparaît de tout filtre réaliste
        (`surface_max=100` l'exclut), remonte en tête d'un tri par surface
        décroissante, et est comptée dans `surface_min=600`. Aucune erreur, aucun
        log — juste un résultat faux. Comportement ACTUEL figé ici.
        """
        search_id = text_surface_dataset["id"]

        assert "virgule" in self._ids(storage, search_id, surface_min=600)
        assert "virgule" not in self._ids(storage, search_id, surface_min=60, surface_max=100)
        # La colonne, elle, contient bien la valeur d'origine : seul le filtre
        # est faux, pas l'affichage.
        assert [
            row["surface"]
            for row in storage.listings.get_listings_for_search(search_id, limit=100)
            if row["listing_id"] == "virgule"
        ] == ["65,5 m²"]

    @pytest.mark.parametrize(
        "listing_id",
        [
            pytest.param("vide", id="chaine-vide"),
            pytest.param("texte", id="texte-sans-chiffre"),
            pytest.param("propre", id="colonne-a-null"),
            pytest.param("nul", id="ligne-inseree-a-null"),
        ],
    )
    def test_values_that_hold_no_number_are_excluded_by_any_numeric_filter(
        self, storage, text_surface_dataset, listing_id,
    ):
        """Les trois chemins qui mènent à NULL — colonne NULL, chaîne vide, et
        texte dont le nettoyage ne laisse rien (`NULLIF(..., '')`) — se
        comportent identiquement : la comparaison rend NULL, donc la ligne est
        écartée. Ni erreur, ni inclusion par défaut.
        """
        search_id = text_surface_dataset["id"]

        assert listing_id not in self._ids(storage, search_id, surface_min=0)
        assert listing_id not in self._ids(storage, search_id, surface_max=100_000)
        assert listing_id not in self._ids(storage, search_id, rooms_min=0)
        # Sans filtre numérique, la ligne est bien là : c'est le filtre qui
        # l'exclut, pas la jointure.
        assert listing_id in self._ids(storage, search_id)

    def test_a_surface_min_of_zero_still_excludes_the_unparseable_rows(self, storage, text_surface_dataset):
        """`surface_min=0` est un filtre *actif* (le constructeur teste
        `is not None`), et il n'est donc pas neutre : il retire toutes les
        annonces sans surface exploitable. Une case « surface minimum » laissée à
        0 dans un formulaire réduit silencieusement le résultat."""
        search_id = text_surface_dataset["id"]

        assert self._ids(storage, search_id, surface_min=0) == {"unite", "decimal", "virgule"}
        assert len(self._ids(storage, search_id)) == 7

    def test_a_value_with_two_dots_breaks_the_whole_query(self, storage, search, sql):
        """# BUG : le nettoyage conserve les points, donc une valeur comme
        « 1.200.50 » (ou un simple « . ») produit une chaîne que `NUMERIC` refuse.
        Le `CAST` est dans le `WHERE` : il est évalué sur les lignes parcourues,
        pas seulement sur celles qui matchent. **Une seule** annonce malformée
        fait donc échouer la page entière — liste ET comptage — pour tous les
        filtres de surface, et pas seulement pour cette annonce.

        Rien ne valide `surface` à l'écriture (c'est du TEXT libre venu du HTML),
        donc rien n'empêche une source d'en produire. Comportement ACTUEL figé.
        """
        storage.listings.save_and_link(
            [
                make_listing(listing_id="saine", surface="65 m²"),
                make_listing(listing_id="malformee", surface="1.200.50 m²"),
            ],
            search["id"],
        )
        # Sans filtre numérique, tout va bien : le CAST n'est pas évalué.
        assert len(storage.listings.get_listings_for_search(search["id"], limit=100)) == 2

        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            storage.listings.get_listings_for_search(search["id"], limit=100, filters={"surface_min": 10})

        # Le comptage tombe de la même façon — la pagination est inutilisable.
        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            storage.listings.count_listings_for_search(search["id"], filters={"surface_min": 10})

        # La connexion, elle, reste utilisable après le rollback du pool.
        assert storage.listings.count_listings_for_search(search["id"]) == 2
        assert sql.one("SELECT COUNT(*) FROM listings") == 2


# ---------------------------------------------------------------------------
# Parité liste / comptage sur de vraies lignes
# ---------------------------------------------------------------------------

@pytest.fixture
def parity_dataset(storage, search, other_search):
    """Un jeu volontairement hétérogène : agences, villes, DPE, booléens,
    surfaces textuelles et prix manquants. Une annonce est aussi liée à la
    recherche d'un autre utilisateur, pour que la jointure sur `search_id` ait
    quelque chose à exclure.
    """
    listings = [
        make_listing(
            listing_id="a", title="Loft lumineux", price_value=900.0, surface="30 m²", rooms="1",
            city="Paris", district="13e", zip_code="75013", agency="Foncia", epc="C", ges="B",
            property_type="apartment", is_private=False, is_new=True, creation_date="2026-01-15",
            description="proche métro", location="Paris 13e",
        ),
        make_listing(
            listing_id="b", title="Duplex", price_value=1500.0, surface="80 m²", rooms="4",
            city="Paris", district="14e", zip_code="75014", agency="Nexity", epc="D", ges="C",
            property_type="apartment", is_private=True, is_new=False, creation_date="2026-03-01",
            description="loft mansardé", location="Paris 14e",
        ),
        make_listing(
            listing_id="c", title="Maison", price_value=None, surface="", rooms="",
            city="Bordeaux", district="", zip_code="33000", agency="", epc="", ges="",
            property_type="house", is_private=False, is_new=False, creation_date="",
            description="", location="Bordeaux",
        ),
        make_listing(
            listing_id="d", title="Studio", price_value=650.0, surface="18 m²", rooms="1",
            city="Poitiers", district="Centre", zip_code="86000", agency="Foncia", epc="F", ges="F",
            property_type="apartment", is_private=True, is_new=True, creation_date="2026-02-20",
            description="loft étudiant", location="Poitiers centre",
        ),
    ]
    storage.listings.save_and_link(listings, search["id"])
    storage.listings.save_and_link([make_listing(listing_id="ailleurs")], other_search["id"])
    return search


PARITY_CASES = [
    pytest.param({}, None, id="aucun-filtre"),
    pytest.param({"q": "loft"}, None, id="q-sur-quatre-colonnes"),
    pytest.param({"q": "LOFT"}, None, id="q-insensible-a-la-casse"),
    pytest.param({"q": "inexistant"}, None, id="q-sans-resultat"),
    pytest.param({"price_min": 800, "price_max": 1600}, None, id="fourchette-de-prix"),
    pytest.param({"price_min": 0}, None, id="prix-min-nul"),
    pytest.param({"surface_min": 20, "surface_max": 100}, None, id="surface-textuelle"),
    pytest.param({"rooms_min": 1, "rooms_max": 2}, None, id="pieces-textuelles"),
    pytest.param({"city": "par"}, None, id="ville-fragment-ilike"),
    pytest.param({"district": "13e"}, None, id="quartier"),
    pytest.param({"zip_code": "75013"}, None, id="code-postal-exact"),
    pytest.param({"property_type": "house"}, None, id="type-de-bien"),
    pytest.param({"agency": "Foncia"}, None, id="agence-exacte"),
    pytest.param({"epc": "C", "ges": "B"}, None, id="dpe-et-ges"),
    pytest.param({"is_private": True}, None, id="particulier-vrai"),
    pytest.param({"is_private": False, "is_new": False}, None, id="booleens-faux"),
    pytest.param({"date_min": "2026-02-01"}, None, id="date-de-creation-texte"),
    pytest.param({}, ["Foncia"], id="blacklist-une-agence"),
    pytest.param({}, ["Foncia", "Nexity"], id="blacklist-deux-agences"),
    pytest.param({"q": "loft", "surface_min": 20, "is_new": True}, ["Nexity"], id="tout-combine"),
]


class TestListCountParity:
    @pytest.mark.parametrize(("filters", "blacklist"), PARITY_CASES)
    def test_the_count_equals_the_number_of_rows_returned(self, storage, parity_dataset, filters, blacklist):
        """🔒 L'invariant de la pagination : `count_listings_for_search` et
        `get_listings_for_search` reconstruisent leur `WHERE` séparément. Le test
        unitaire compare les deux chaînes SQL ; ici on compare les deux
        *résultats* sur de vraies lignes — la seule façon de prendre une
        divergence que le moteur interprète (NULL, casse, CAST) alors que les
        deux chaînes sont identiques.
        """
        search_id = parity_dataset["id"]

        rows = storage.listings.get_listings_for_search(
            search_id, limit=1000, offset=0, blacklisted_agencies=blacklist, filters=filters,
        )
        count = storage.listings.count_listings_for_search(
            search_id, blacklisted_agencies=blacklist, filters=filters,
        )

        assert count == len(rows)

    @pytest.mark.parametrize(("filters", "expected"), [
        pytest.param({}, {"a", "b", "c", "d"}, id="tout"),
        pytest.param({"q": "loft"}, {"a", "b", "d"}, id="q-touche-titre-et-description"),
        pytest.param({"price_min": 800}, {"a", "b"}, id="prix-min-exclut-le-null"),
        pytest.param({"price_max": 700}, {"d"}, id="prix-max"),
        pytest.param({"city": "par"}, {"a", "b"}, id="ville-fragment"),
        pytest.param({"zip_code": "7501"}, set(), id="code-postal-est-une-egalite"),
        pytest.param({"property_type": "apartment"}, {"a", "b", "d"}, id="type-de-bien"),
        pytest.param({"is_private": True}, {"b", "d"}, id="particulier"),
        pytest.param({"is_new": False}, {"b", "c"}, id="pas-neuf"),
        pytest.param({"epc": "F"}, {"d"}, id="dpe"),
        pytest.param({"date_min": "2026-02-01"}, {"b", "d"}, id="date-comparee-en-texte"),
        pytest.param({"agency": "foncia"}, set(), id="agence-sensible-a-la-casse"),
    ])
    def test_each_filter_selects_the_expected_rows(self, storage, parity_dataset, filters, expected):
        """Complément de la parité : la même requête doit rendre les BONNES
        lignes, pas seulement un compte cohérent. `date_min` est comparé sur une
        colonne TEXT — l'ordre lexicographique coïncide avec l'ordre
        chronologique uniquement parce que le format est ISO."""
        rows = storage.listings.get_listings_for_search(parity_dataset["id"], limit=1000, filters=filters)

        assert {row["listing_id"] for row in rows} == expected

    def test_the_join_never_leaks_another_searchs_listings(self, storage, parity_dataset, other_search):
        rows = storage.listings.get_listings_for_search(parity_dataset["id"], limit=1000)

        assert "ailleurs" not in {row["listing_id"] for row in rows}
        assert storage.listings.count_listings_for_search(other_search["id"]) == 1

    def test_blacklisted_agencies_excludes_them_but_keeps_the_empty_agency(self, storage, parity_dataset):
        """`agency NOT IN (...)` est faux pour NULL mais vrai pour la chaîne
        vide : l'annonce `c` (agence `''`) survit à la blacklist. C'est le
        comportement voulu — une annonce de particulier n'a pas d'agence à
        exclure — mais il repose entièrement sur le fait que la colonne a
        `DEFAULT ''` et non NULL."""
        rows = storage.listings.get_listings_for_search(
            parity_dataset["id"], limit=1000, blacklisted_agencies=["Foncia", "Nexity"],
        )

        assert {row["listing_id"] for row in rows} == {"c"}

    def test_pagination_walks_every_row_exactly_once(self, storage, parity_dataset):
        search_id = parity_dataset["id"]
        total = storage.listings.count_listings_for_search(search_id)
        seen = []

        for offset in range(0, total, 2):
            page = storage.listings.get_listings_for_search(search_id, limit=2, offset=offset)
            seen.extend(row["listing_id"] for row in page)

        assert len(seen) == total == 4
        assert len(set(seen)) == total

    def test_an_offset_past_the_end_is_empty_and_the_count_is_unchanged(self, storage, parity_dataset):
        search_id = parity_dataset["id"]

        assert storage.listings.get_listings_for_search(search_id, limit=10, offset=100) == []
        assert storage.listings.count_listings_for_search(search_id) == 4

    def test_the_rows_carry_the_link_date_alongside_the_listing_columns(self, storage, parity_dataset):
        """`SELECT l.*, sl.found_at` : la date affichée est celle de la
        découverte pour CETTE recherche, pas le `first_seen` global de
        l'annonce."""
        row = storage.listings.get_listings_for_search(parity_dataset["id"], limit=1)[0]

        assert row["found_at"] is not None
        assert row["first_seen"] is not None
        assert "listing_id" in row and "price_value" in row


# ---------------------------------------------------------------------------
# Les 8 tris
# ---------------------------------------------------------------------------

@pytest.fixture
def sortable_dataset(storage, search, sql):
    """Trois annonces dont chaque colonne triable est ordonnée a < b, et une
    troisième (`c`) dont toutes les valeurs sont NULL ou vides — c'est elle qui
    prouve les `NULLS LAST`.

    `found_at` est vieilli en SQL : toutes les lignes d'un même `save_and_link`
    partagent le `CURRENT_TIMESTAMP` de la transaction, l'ordre serait donc
    indéterminé sans ça.
    """
    storage.listings.save_and_link(
        [
            make_listing(listing_id="a", price_value=900.0, surface="30 m²", creation_date="2026-01-01"),
            make_listing(listing_id="b", price_value=1500.0, surface="80 m²", creation_date="2026-03-01"),
            make_listing(listing_id="c", price_value=None, surface="", creation_date=None),
        ],
        search["id"],
    )
    sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '3 days' WHERE listing_id = 'a'")
    sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '2 days' WHERE listing_id = 'b'")
    sql.exec("UPDATE search_listings SET found_at = NOW() - INTERVAL '1 day' WHERE listing_id = 'c'")
    return search


class TestSorting:
    @pytest.mark.parametrize(
        ("sort", "expected"),
        [
            pytest.param("found_at_desc", ["c", "b", "a"], id="found_at_desc"),
            pytest.param("found_at_asc", ["a", "b", "c"], id="found_at_asc"),
            pytest.param("price_asc", ["a", "b", "c"], id="price_asc-nulls-last"),
            pytest.param("price_desc", ["b", "a", "c"], id="price_desc-nulls-last"),
            pytest.param("surface_asc", ["a", "b", "c"], id="surface_asc-cast-nulls-last"),
            pytest.param("surface_desc", ["b", "a", "c"], id="surface_desc-cast-nulls-last"),
            pytest.param("date_asc", ["a", "b", "c"], id="date_asc-nulls-last"),
            pytest.param("date_desc", ["b", "a", "c"], id="date_desc-nulls-last"),
        ],
    )
    def test_each_of_the_eight_sorts_produces_the_right_order(self, storage, sortable_dataset, sort, expected):
        """Les 8 entrées de l'allowlist, exécutées pour de vrai. L'unitaire
        vérifie la *chaîne* produite ; seul le moteur dit si `NULLS LAST` place
        bien l'annonce sans prix en dernier dans les DEUX sens — c'est le piège
        classique : `DESC` place les NULL en premier par défaut, et un `NULLS
        LAST` oublié sur une seule des deux directions ne se voit pas dans une
        revue de code.
        """
        rows = storage.listings.get_listings_for_search(sortable_dataset["id"], limit=10, sort=sort)

        assert [row["listing_id"] for row in rows] == expected

    @pytest.mark.parametrize("hostile", ["", "inconnu", "PRICE_ASC", "; DROP TABLE listings", None])
    def test_an_unknown_sort_falls_back_to_the_most_recent_first(self, storage, sortable_dataset, hostile):
        """L'allowlist est l'unique défense (`sort` est interpolé en f-string) :
        contre le vrai moteur, une valeur hostile doit produire une requête
        valide et l'ordre par défaut, pas une erreur de syntaxe."""
        rows = storage.listings.get_listings_for_search(sortable_dataset["id"], limit=10, sort=hostile)

        assert [row["listing_id"] for row in rows] == ["c", "b", "a"]

    def test_sorting_and_filtering_compose(self, storage, sortable_dataset):
        rows = storage.listings.get_listings_for_search(
            sortable_dataset["id"], limit=10, sort="price_desc", filters={"price_min": 1000},
        )

        assert [row["listing_id"] for row in rows] == ["b"]

    def test_the_sort_survives_pagination(self, storage, sortable_dataset):
        search_id = sortable_dataset["id"]

        first = storage.listings.get_listings_for_search(search_id, limit=2, offset=0, sort="price_asc")
        second = storage.listings.get_listings_for_search(search_id, limit=2, offset=2, sort="price_asc")

        assert [row["listing_id"] for row in first] == ["a", "b"]
        assert [row["listing_id"] for row in second] == ["c"]


# ---------------------------------------------------------------------------
# get_filter_options
# ---------------------------------------------------------------------------

class TestGetFilterOptions:
    def test_options_are_distinct_sorted_and_exclude_null_and_empty(self, storage, parity_dataset):
        """Chacune des sept colonnes est interrogée avec `DISTINCT ... IS NOT
        NULL AND != '' ORDER BY`. Les valeurs vides (fréquentes : la colonne a
        `DEFAULT ''`) ne doivent pas apparaître comme une option cliquable
        vide."""
        options = storage.listings.get_filter_options(parity_dataset["id"])

        assert options["city"] == ["Bordeaux", "Paris", "Poitiers"]
        assert options["district"] == ["13e", "14e", "Centre"]
        assert options["zip_code"] == ["33000", "75013", "75014", "86000"]
        assert options["property_type"] == ["apartment", "house"]
        assert options["agency"] == ["Foncia", "Nexity"]
        assert options["epc"] == ["C", "D", "F"]
        assert options["ges"] == ["B", "C", "F"]

    def test_the_boolean_facets_come_from_filtered_counts(self, storage, parity_dataset):
        options = storage.listings.get_filter_options(parity_dataset["id"])

        assert options["has_private"] is True
        assert options["has_non_private"] is True
        assert options["has_new"] is True
        assert options["has_not_new"] is True

    def test_a_facet_with_no_matching_row_is_false(self, storage, search):
        storage.listings.save_and_link(
            [make_listing(listing_id="a", is_private=False, is_new=False)], search["id"],
        )

        options = storage.listings.get_filter_options(search["id"])

        assert options["has_private"] is False
        assert options["has_non_private"] is True
        assert options["has_new"] is False
        assert options["has_not_new"] is True

    def test_a_search_without_listings_yields_empty_lists_and_false_facets(self, storage, search):
        options = storage.listings.get_filter_options(search["id"])

        assert all(
            options[col] == []
            for col in ("city", "district", "zip_code", "property_type", "agency", "epc", "ges")
        )
        assert not any(options[key] for key in ("has_private", "has_non_private", "has_new", "has_not_new"))

    def test_options_are_scoped_to_the_search(self, storage, parity_dataset, other_search):
        storage.listings.save_and_link(
            [make_listing(listing_id="autre", city="Lille")], other_search["id"],
        )

        assert "Lille" not in storage.listings.get_filter_options(parity_dataset["id"])["city"]


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------

class TestDeleteOldListings:
    def test_make_interval_runs_and_spares_fresh_listings(self, storage, search):
        """Régression : `INTERVAL '%s days'` n'était pas du SQL valide (psycopg2
        cite à l'intérieur du littéral). `make_interval(days => %s)` prend le
        nombre en paramètre — ça ne se vérifie qu'en l'exécutant."""
        storage.listings.save_and_link([make_listing(listing_id="fraiche")], search["id"])

        assert storage.listings.delete_old_listings(days=4) == 0

        assert storage.listings.count_listings_for_search(search["id"]) == 1

    @pytest.mark.parametrize("days", [1, 4, 30, 0])
    def test_the_cutoff_is_a_real_parameter(self, storage, search, sql, days):
        storage.listings.save_and_link([make_listing(listing_id="vieille")], search["id"])
        sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '10 days'")

        deleted = storage.listings.delete_old_listings(days=days)

        assert deleted == (1 if days <= 10 else 0)

    def test_deleting_an_old_listing_cascades_to_its_links(self, storage, search, sql):
        """`search_listings.listing_id` a un `ON DELETE CASCADE` : la purge
        n'émet qu'un `DELETE FROM listings`, c'est le moteur qui nettoie les
        liens. Sans la cascade, le `DELETE` échouerait sur la clé étrangère et
        la purge ne supprimerait jamais rien."""
        storage.listings.save_and_link(
            [make_listing(listing_id="vieille"), make_listing(listing_id="recente")], search["id"],
        )
        sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '10 days' WHERE listing_id = 'vieille'")

        assert storage.listings.delete_old_listings(days=4) == 1

        assert sql.one("SELECT COUNT(*) FROM search_listings") == 1
        assert storage.listings.count_listings_for_search(search["id"]) == 1

    def test_it_deletes_across_all_searches_and_users(self, storage, search, other_search, sql):
        """La purge est globale : elle n'a pas de `user_id`. Une annonce vieille
        disparaît même si un autre utilisateur la suit encore."""
        storage.listings.save_and_link([make_listing(listing_id="partagee")], search["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], other_search["id"])
        sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '10 days'")

        assert storage.listings.delete_old_listings(days=4) == 1

        assert sql.one("SELECT COUNT(*) FROM search_listings") == 0

    def test_nothing_to_delete_returns_zero(self, storage):
        assert storage.listings.delete_old_listings(days=4) == 0


class TestDeleteListing:
    def test_deleting_returns_true_and_cascades(self, storage, search, sql):
        storage.listings.save_and_link(
            [make_listing(listing_id="a"), make_listing(listing_id="b")], search["id"],
        )

        assert storage.listings.delete_listing("a") is True

        assert sql.one("SELECT COUNT(*) FROM listings") == 1
        assert sql.one("SELECT COUNT(*) FROM search_listings WHERE listing_id = 'a'") == 0

    def test_deleting_an_unknown_listing_returns_false(self, storage):
        assert storage.listings.delete_listing("jamais_vue") is False

    def test_the_search_itself_is_untouched(self, storage, search):
        storage.listings.save_and_link([make_listing(listing_id="a")], search["id"])

        storage.listings.delete_listing("a")

        assert storage.searches.get_search(search["id"])["label"] == "Paris 13e"


class TestOrphanListings:
    def test_a_listing_becomes_an_orphan_when_its_last_search_is_deleted(self, storage, user, sql):
        """Le scénario réel : `delete_search` cascade sur `search_listings` mais
        PAS sur `listings` (l'annonce peut appartenir à une autre recherche).
        L'annonge devient orpheline, et c'est ce ménage-ci qui la récupère."""
        doomed = insert_search(storage, user["id"], "À supprimer")
        keeper = insert_search(storage, user["id"], "À garder")
        storage.listings.save_and_link(
            [make_listing(listing_id="orpheline"), make_listing(listing_id="partagee")], doomed["id"],
        )
        storage.listings.save_and_link([make_listing(listing_id="partagee")], keeper["id"])

        storage.searches.delete_search(doomed["id"])

        assert storage.listings.get_orphan_listings_count() == 1
        assert storage.listings.delete_orphan_listings() == 1
        assert sql.one("SELECT listing_id FROM listings") == "partagee"

    def test_counting_orphans_does_not_delete_them(self, storage, user):
        doomed = insert_search(storage, user["id"], "À supprimer")
        storage.listings.save_and_link([make_listing(listing_id="orpheline")], doomed["id"])
        storage.searches.delete_search(doomed["id"])

        assert storage.listings.get_orphan_listings_count() == 1
        assert storage.listings.get_orphan_listings_count() == 1

    def test_no_orphan_means_zero_and_no_deletion(self, storage, search):
        storage.listings.save_and_link([make_listing(listing_id="liee")], search["id"])

        assert storage.listings.get_orphan_listings_count() == 0
        assert storage.listings.delete_orphan_listings() == 0
        assert storage.listings.count_listings_for_search(search["id"]) == 1

    def test_linked_listings_are_never_collateral_damage(self, storage, search, sql):
        sql.exec("INSERT INTO listings (listing_id, url) VALUES ('orpheline', 'http://x')")
        storage.listings.save_and_link([make_listing(listing_id="liee")], search["id"])

        assert storage.listings.delete_orphan_listings() == 1

        assert sql.one("SELECT listing_id FROM listings") == "liee"


# ---------------------------------------------------------------------------
# Vue admin : toutes les annonces
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_dataset(storage, search, other_search, sql):
    storage.listings.save_and_link(
        [
            make_listing(listing_id="a", title="Loft", source="seloger", location="Paris 13e"),
            make_listing(
                listing_id="b", title="Maison", source="laforet",
                description="grand loft", location="Bordeaux",
            ),
        ],
        search["id"],
    )
    storage.listings.save_and_link(
        [
            make_listing(listing_id="a"),
            make_listing(listing_id="c", title="Studio", source="laforet", location="Poitiers"),
        ],
        other_search["id"],
    )
    sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '3 days' WHERE listing_id = 'a'")
    sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '2 days' WHERE listing_id = 'b'")
    sql.exec("UPDATE listings SET first_seen = NOW() - INTERVAL '1 day' WHERE listing_id = 'c'")
    return


ADMIN_FILTER_CASES = [
    pytest.param("", "", {"a", "b", "c"}, id="aucun-filtre"),
    pytest.param("loft", "", {"a", "b"}, id="terme-sur-titre-et-description"),
    pytest.param("LOFT", "", {"a", "b"}, id="terme-insensible-a-la-casse"),
    pytest.param("paris", "", {"a"}, id="terme-sur-location"),
    pytest.param("introuvable", "", set(), id="terme-sans-resultat"),
    pytest.param("", "laforet", {"b", "c"}, id="source-exacte"),
    pytest.param("", "LAFORET", set(), id="source-sensible-a-la-casse"),
    pytest.param("loft", "laforet", {"b"}, id="les-deux-filtres"),
    pytest.param("loft", "inconnue", set(), id="filtres-contradictoires"),
    # Le terme est interpolé en `%<valeur>%` puis paramétré : pas d'injection,
    # mais les jokers LIKE ne sont pas échappés.
    pytest.param("%", "", {"a", "b", "c"}, id="pourcent-joker-non-echappe"),
]


class TestAllListings:
    @pytest.mark.parametrize(("term", "source", "expected"), ADMIN_FILTER_CASES)
    def test_the_admin_count_matches_the_admin_rows(self, storage, admin_dataset, term, source, expected):
        """Même invariant de pagination que côté recherche, sur l'autre paire de
        méthodes. `count_all_listings` compte des `DISTINCT l.listing_id` alors
        que `get_all_listings` ne joint pas : les deux ne peuvent coïncider que
        parce que `listing_id` est la clé primaire — ce test le vérifie sur une
        annonce liée à DEUX recherches (`a`), qui serait comptée deux fois par
        une jointure mal placée."""
        rows = storage.listings.get_all_listings(limit=1000, search_term=term, source_filter=source)
        count = storage.listings.count_all_listings(search_term=term, source_filter=source)

        assert {row["listing_id"] for row in rows} == expected
        assert count == len(rows) == len(expected)

    def test_each_row_carries_its_number_of_linked_searches(self, storage, admin_dataset):
        """Le sous-select corrélé `(SELECT COUNT(*) FROM search_listings WHERE
        listing_id = l.listing_id)` : c'est ce qui permet à l'admin de repérer
        une annonce partagée avant de la supprimer."""
        rows = {row["listing_id"]: row["linked_searches"] for row in storage.listings.get_all_listings(limit=1000)}

        assert rows == {"a": 2, "b": 1, "c": 1}

    def test_rows_are_ordered_by_first_seen_descending(self, storage, admin_dataset):
        rows = storage.listings.get_all_listings(limit=1000)

        assert [row["listing_id"] for row in rows] == ["c", "b", "a"]

    def test_pagination_walks_every_row_exactly_once(self, storage, admin_dataset):
        seen = []
        for offset in (0, 2):
            seen.extend(row["listing_id"] for row in storage.listings.get_all_listings(limit=2, offset=offset))

        assert seen == ["c", "b", "a"]

    def test_an_empty_database_yields_no_rows_and_a_zero_count(self, storage):
        assert storage.listings.get_all_listings() == []
        assert storage.listings.count_all_listings() == 0

    def test_an_orphan_listing_still_shows_up_with_zero_links(self, storage, sql):
        """La vue admin n'est pas jointe à `search_listings` : c'est exactement
        ce qui rend les orphelines visibles et supprimables depuis l'admin."""
        sql.exec("INSERT INTO listings (listing_id, url) VALUES ('orpheline', 'http://x')")

        (row,) = storage.listings.get_all_listings()

        assert row["listing_id"] == "orpheline"
        assert row["linked_searches"] == 0


# ---------------------------------------------------------------------------
# Détail d'une annonce
# ---------------------------------------------------------------------------

class TestListingDetail:
    def test_the_detail_joins_every_search_and_its_owner(self, storage, user, other_user):
        mine = insert_search(storage, user["id"], "À moi", source="seloger")
        theirs = insert_search(storage, other_user["id"], "À bob", source="laforet")
        storage.listings.save_and_link([make_listing(listing_id="partagee", title="Studio")], mine["id"])
        storage.listings.save_and_link([make_listing(listing_id="partagee")], theirs["id"])

        detail = storage.listings.get_listing_detail("partagee")

        assert detail["title"] == "Studio"
        assert {(s["label"], s["username"], s["source"]) for s in detail["linked_searches"]} == {
            ("À moi", "alice", "seloger"),
            ("À bob", "bob", "laforet"),
        }
        assert {s["id"] for s in detail["linked_searches"]} == {mine["id"], theirs["id"]}

    def test_an_orphan_listing_has_an_empty_linked_searches_list(self, storage, sql):
        sql.exec("INSERT INTO listings (listing_id, url) VALUES ('orpheline', 'http://x')")

        detail = storage.listings.get_listing_detail("orpheline")

        assert detail["listing_id"] == "orpheline"
        assert detail["linked_searches"] == []

    def test_an_unknown_listing_returns_none(self, storage):
        assert storage.listings.get_listing_detail("jamais_vue") is None

    def test_the_jsonb_columns_come_back_as_python_objects(self, storage, search):
        """`phone` et `photos` sont des colonnes JSONB alimentées avec des
        *chaînes* JSON par les parsers : psycopg2 les désérialise à la lecture,
        donc l'appelant reçoit une liste, pas la chaîne qu'il a écrite."""
        storage.listings.save_and_link(
            [make_listing(
                listing_id="sl_1",
                phone='["0102030405"]',
                photos='[{"url": "https://cdn.example/p0.jpg", "alt": "Photo 0"}]',
            )],
            search["id"],
        )

        detail = storage.listings.get_listing_detail("sl_1")

        assert detail["phone"] == ["0102030405"]
        assert detail["photos"] == [{"url": "https://cdn.example/p0.jpg", "alt": "Photo 0"}]


# ---------------------------------------------------------------------------
# get_unique_agencies_for_user
# ---------------------------------------------------------------------------

class TestUniqueAgenciesForUser:
    def test_agencies_are_distinct_sorted_and_scoped_to_the_user(self, storage, user, other_user):
        """Jointure à trois tables (`listings` → `search_listings` → `searches`)
        filtrée sur `s.user_id` : c'est ce qui alimente la liste de blacklist
        d'un utilisateur, qui ne doit contenir que des agences qu'il a vraiment
        croisées."""
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")
        theirs = insert_search(storage, other_user["id"], "Chez bob")
        storage.listings.save_and_link(
            [
                make_listing(listing_id="a", agency="Orpi"),
                make_listing(listing_id="b", agency="Foncia"),
                make_listing(listing_id="c", agency="Foncia"),
            ],
            first["id"],
        )
        storage.listings.save_and_link([make_listing(listing_id="d", agency="Century 21")], second["id"])
        storage.listings.save_and_link([make_listing(listing_id="e", agency="Nexity")], theirs["id"])

        assert storage.listings.get_unique_agencies_for_user(user["id"]) == ["Century 21", "Foncia", "Orpi"]
        assert storage.listings.get_unique_agencies_for_user(other_user["id"]) == ["Nexity"]

    def test_empty_and_null_agencies_are_excluded(self, storage, user, search, sql):
        """La colonne a `DEFAULT ''` : sans le `!= ''`, la liste de blacklist
        proposerait une entrée vide, impossible à distinguer d'une agence
        réelle."""
        storage.listings.save_and_link(
            [
                make_listing(listing_id="vide", agency=""),
                make_listing(listing_id="nulle", agency="Orpi"),
            ],
            search["id"],
        )
        sql.exec("UPDATE listings SET agency = NULL WHERE listing_id = 'nulle'")

        assert storage.listings.get_unique_agencies_for_user(user["id"]) == []

    def test_a_user_without_listings_gets_an_empty_list(self, storage, user):
        assert storage.listings.get_unique_agencies_for_user(user["id"]) == []
        assert storage.listings.get_unique_agencies_for_user(999_999) == []
