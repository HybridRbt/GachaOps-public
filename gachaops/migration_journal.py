"""Prepared/complete transaction journal for registration migration and import.

One data root allows at most one pending transaction. While a transaction is
prepared, mixed state must not be exposed and normal configuration writes are
refused. The journal lives in config/registration-migration.json and is
archived into its backup directory once complete.
"""
import json
from pathlib import Path

from .planner import ConfigError
from .recovery import save

JOURNAL_NAME = 'registration-migration.json'
JOURNAL_KIND = 'registration_transaction_journal'
BACKUP_DIRECTORY = 'backups/registration-migrations'


def journal_path(data_root):
    return Path(data_root).resolve() / 'config' / JOURNAL_NAME


def backup_root(data_root):
    return Path(data_root).resolve() / BACKUP_DIRECTORY


def read_journal(data_root):
    """Validated journal document, or None when no journal file exists."""
    path = journal_path(data_root)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ConfigError('external migration journal')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        raise ConfigError(f'迁移事务记录损坏：{error}') from None
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('kind') != JOURNAL_KIND
            or value.get('state') not in {'prepared', 'complete'}
            or value.get('transaction') not in {'migration', 'import'}
            or not isinstance(value.get('backup'), str) or not value['backup']
            or not isinstance(value.get('manifest_sha256'), str)):
        raise ConfigError('invalid migration journal schema')
    return value


def archive_journal(data_root, journal):
    """File a complete receipt into its backup directory and remove the journal."""
    if journal.get('state') != 'complete':
        raise ConfigError('only complete transactions are archived')
    destination = backup_root(data_root) / journal['backup'] / 'complete.json'
    if destination.exists():
        # Already archived (a crash between archive steps); keep the receipt.
        journal_path(data_root).unlink(missing_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    save(destination, journal)
    journal_path(data_root).unlink(missing_ok=True)


def require_no_prepared_journal(data_root):
    """Gate for normal configuration writes; call while holding the config lock."""
    journal = read_journal(data_root)
    if journal is None:
        return
    if journal['state'] == 'prepared':
        raise ConfigError('登记迁移/导入尚未完成；请先在启动提示中继续或处理该事务')
    archive_journal(data_root, journal)
