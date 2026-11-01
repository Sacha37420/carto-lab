"""Champ de modèle chiffré au repos — coffre de l'app (`VAULT_ENCRYPTION_KEY`).

Clé : `settings.VAULT_ENCRYPTION_KEY`, clé Fernet propre au coffre, sans lien
avec `SECRET_KEY`. Avant le 2026-10-31, la clé était dérivée de `SECRET_KEY` :
faire tourner `SECRET_KEY` détruisait en silence tous les secrets, et une fuite
de `SECRET_KEY` les livrait. L'ancienne clé n'est plus que LUE (`MultiFernet`,
le temps que la migration de données `rechiffrer_coffre` ait tout réécrit).

Une valeur qui ne se déchiffre sous aucune des deux clés n'est plus rendue
telle quelle (c'était le texte chiffré, présenté comme un secret valide) : elle
est traitée comme absente (`''`) et journalisée sans sa valeur. Seule
exception : une valeur qui n'a pas la forme d'un jeton Fernet (`gAAAAA…`) est
une valeur historique en clair (antérieure au chiffrement) — rendue telle
quelle, et rechiffrée par la migration.

Fichier identique dans restauration et robot-lab (chantier « failles », I3).
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

# Tout jeton Fernet commence par l'octet de version 0x80 suivi de l'horodatage :
# en base64 urlsafe, « gAAAAA ».
FERNET_PREFIX = 'gAAAAA'


def _legacy_fernet() -> Fernet:
    """Ancienne clé, dérivée de SECRET_KEY — lecture seule."""
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def vault_fernet() -> Fernet:
    """Clé du coffre (`VAULT_ENCRYPTION_KEY`). Lève si elle est absente ou mal formée."""
    key = getattr(settings, 'VAULT_ENCRYPTION_KEY', '') or ''
    if not key:
        raise RuntimeError('VAULT_ENCRYPTION_KEY absente : le coffre ne peut ni chiffrer ni déchiffrer.')
    return Fernet(key.encode() if isinstance(key, str) else key)


def _reader() -> MultiFernet:
    """Chiffre sous la clé du coffre ; déchiffre sous la clé du coffre, puis l'ancienne."""
    return MultiFernet([vault_fernet(), _legacy_fernet()])


def encrypt(plaintext: str) -> str:
    return vault_fernet().encrypt(plaintext.encode()).decode()


def is_fernet_token(value: str) -> bool:
    return isinstance(value, str) and value.startswith(FERNET_PREFIX)


def decrypt_stored(value, where: str = '?'):
    """Valeur en base → valeur en clair, ou `''` si indéchiffrable.

    `where` ne sert qu'au journal (`Modèle.champ`) ; la valeur n'y figure jamais.
    """
    if value in (None, ''):
        return value
    try:
        return _reader().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        if not is_fernet_token(value):
            return value  # valeur historique en clair
        logger.warning('Coffre : valeur indéchiffrable dans %s, traitée comme absente.', where)
        return ''


class EncryptedTextField(models.TextField):
    """Chiffre la valeur avant écriture en base et la déchiffre à la lecture."""

    def _where(self) -> str:
        model = getattr(self, 'model', None)
        return f"{getattr(model, '__name__', '?')}.{self.name}"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ''):
            return value
        return encrypt(value)

    def from_db_value(self, value, expression, connection):
        return decrypt_stored(value, self._where())


def reencrypt_raw(raw):
    """Valeur brute en base → valeur brute sous la clé du coffre, ou `None` si rien à écrire.

    Pour la migration de données. Ne touche pas une valeur déjà sous la clé du
    coffre, ni une valeur indéchiffrable (laissée en base telle quelle : elle se
    lit comme absente, et une clé retrouvée plus tard pourrait encore la lire).
    """
    if raw in (None, ''):
        return None
    try:
        vault_fernet().decrypt(raw.encode())
        return None  # déjà sous la nouvelle clé
    except (InvalidToken, ValueError, TypeError):
        pass
    try:
        plaintext = _legacy_fernet().decrypt(raw.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        if is_fernet_token(raw):
            return None  # indéchiffrable : laissé tel quel
        plaintext = raw  # historique en clair
    return encrypt(plaintext)


def legacy_encrypt_raw(raw):
    """Inverse de `reencrypt_raw` (retour arrière de la migration) : remet sous l'ancienne clé."""
    if raw in (None, ''):
        return None
    try:
        plaintext = vault_fernet().decrypt(raw.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
    return _legacy_fernet().encrypt(plaintext.encode()).decode()


def rewrite_encrypted_columns(apps, schema_editor, models_fields, transform):
    """Réécrit en SQL brut (sans passer par le champ) les colonnes chiffrées.

    `models_fields` : `[('api', 'Modele', ['champ', …]), …]`. Le SQL brut évite
    que `from_db_value`/`get_prep_value` ne s'interposent.
    """
    conn = schema_editor.connection
    qn = conn.ops.quote_name
    counts = {'rewritten': 0}
    for app_label, model_name, field_names in models_fields:
        model = apps.get_model(app_label, model_name)
        table = qn(model._meta.db_table)
        pk = qn(model._meta.pk.column)
        for name in field_names:
            col = qn(model._meta.get_field(name).column)
            with conn.cursor() as cur:
                cur.execute(f'SELECT {pk}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> %s', [''])
                rows = cur.fetchall()
            for row_pk, raw in rows:
                new = transform(raw)
                if new is None:
                    continue
                with conn.cursor() as cur:
                    cur.execute(f'UPDATE {table} SET {col} = %s WHERE {pk} = %s', [new, row_pk])
                counts['rewritten'] += 1
    return counts
