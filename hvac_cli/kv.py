import json
import logging
from cliff.show import ShowOne
from cliff.lister import Lister
from cliff.command import Command
from hvac_cli.cli import CLI
import re
import sys

logger = logging.getLogger(__name__)


class ReadSecretVersion(Exception):
    pass


class SecretVersion(Exception):
    pass


def kvcli_factory(super_args, args):
    cli = CLI(super_args)
    if not args.kv_version:
        mounts = cli.vault.sys.list_mounted_secrets_engines()['data']
        path = args.mount_point + '/'
        assert path in mounts, f'path {path} is not founds in mounts {mounts}'
        args.kv_version = mounts[path]['options']['version']
    if args.kv_version == '1':
        return KVv1CLI(super_args, args)
    else:
        return KVv2CLI(super_args, args)


class KVCLI(CLI):

    def __init__(self, super_args, args):
        super().__init__(super_args)
        self.kv_version = args.kv_version
        self.mount_point = args.mount_point

    @staticmethod
    def sanitize(path):
        def log_sanitation(path, fun):
            new_path, reason = fun(path)
            if new_path != path:
                logger.info(f'{path} replaced by {new_path} to {reason}')
            return new_path

        def user_friendly(path):
            """replace control characters and DEL because they would be
            difficult for the user to type in the CLI or the web UI.
            Also replace % because it is used in URLs to express %20 etc.
            """
            return re.sub(r'[\x00-\x1f%\x7f]', '_', path), user_friendly.__doc__
        path = log_sanitation(path, user_friendly)

        def bug_6282(path):
            "workaround https://github.com/hashicorp/vault/issues/6282"
            return re.sub(r'[#*+(\\[]', '_', path), bug_6282.__doc__
        path = log_sanitation(path, bug_6282)

        def bug_6213(path):
            "workaround https://github.com/hashicorp/vault/issues/6213"
            path = re.sub(r'\s+/', '/', path)
            path = re.sub(r'\s+$', '', path)
            return path, bug_6213.__doc__
        path = log_sanitation(path, bug_6213)

        return path

    def list_secrets(self, path):
        return self.kv.list_secrets(path, mount_point=self.mount_point)['data']['keys']

    def dump(self):
        r = {}
        self._dump(r, '')
        json.dump(r, sys.stdout)

    def _dump(self, r, prefix):
        keys = self.list_secrets(prefix)
        for key in keys:
            path = prefix + key
            if path.endswith('/'):
                self._dump(r, path)
            else:
                r[path] = self.read_secret(path, version=None)

    def load(self, filepath):
        secrets = json.load(open(filepath))
        for k, v in secrets.items():
            self.create_or_update_secret(k, v, cas=None)

    def erase(self, prefix=''):
        keys = self.list_secrets(prefix)
        for key in keys:
            path = prefix + key
            if path.endswith('/'):
                self.erase(path)
            else:
                logger.debug(f'erase {path}')
                self.delete_metadata_and_all_versions(path)


class KVv1CLI(KVCLI):

    def __init__(self, super_args, args):
        super().__init__(super_args, args)
        self.kv = self.vault.secrets.kv.v1

    def delete_metadata_and_all_versions(self, path):
        self.delete(path, versions=None)

    def read_secret_metadata(self, path):
        raise SecretVersion(
            f'{self.mount_point} is KV {self.kv_version} and does not support metadata')

    def update_metadata(self, path, max_version, cas_required):
        raise SecretVersion(
            f'{self.mount_point} is KV {self.kv_version} and does not support metadata')

    def create_or_update_secret(self, path, entry, cas):
        if cas:
            raise SecretVersion(
                f'{self.mount_point} is KV {self.kv_version} and does not support --cas')
        path = self.sanitize(path)
        self.kv.create_or_update_secret(path, entry, mount_point=self.mount_point)

    def patch(self, path, entry):
        raise SecretVersion(
            f'{self.mount_point} is KV {self.kv_version} and does not support patch')

    def read_secret(self, path, version):
        if version:
            raise ReadSecretVersion(
                f'{self.mount_point} is KV {self.kv_version} and does not support --version')
        return self.kv.read_secret(path, mount_point=self.mount_point)['data']

    def delete(self, path, versions):
        if versions:
            raise SecretVersion(
                f'{self.mount_point} is KV {self.kv_version} and does not support --versions')
        self.kv.delete_secret(path, mount_point=self.mount_point)
        return 0

    def undelete(self, path, versions):
        raise SecretVersion(
            f'{self.mount_point} is KV {self.kv_version} and does not support undelete')


class KVv2CLI(KVCLI):

    def __init__(self, super_args, args):
        super().__init__(super_args, args)
        self.kv = self.vault.secrets.kv.v2

    def delete_metadata_and_all_versions(self, path):
        self.kv.delete_metadata_and_all_versions(path, mount_point=self.mount_point)

    def read_secret_metadata(self, path):
        return self.kv.read_secret_metadata(path, mount_point=self.mount_point)

    def update_metadata(self, path, max_versions, cas_required):
        self.kv.update_metadata(path, max_versions, cas_required, mount_point=self.mount_point)
        return self.read_secret_metadata(path)

    def create_or_update_secret(self, path, entry, cas):
        path = self.sanitize(path)
        self.kv.create_or_update_secret(path, entry, cas=cas, mount_point=self.mount_point)

    def patch(self, path, entry):
        path = self.sanitize(path)
        self.kv.patch(path, entry, mount_point=self.mount_point)

    def read_secret(self, path, version):
        return self.kv.read_secret_version(
            path, version=version, mount_point=self.mount_point)['data']['data']

    def delete(self, path, versions):
        if versions:
            self.kv.delete_secret_versions(
                path, versions=versions, mount_point=self.mount_point)
        else:
            self.kv.delete_latest_version_of_secret(path, mount_point=self.mount_point)
        return 0

    def undelete(self, path, versions):
        self.kv.undelete_secret_versions(
            path, versions=versions, mount_point=self.mount_point)
        return 0


class KvCommand(object):

    def set_common_options(self, parser):
        parser.add_argument(
            '--mount-point',
            default='secret',
            help='KV path mount point, as found in vault read /sys/mounts',
        )
        parser.add_argument(
            '--kv-version',
            choices=['1', '2'],
            required=False,
            help=('Force the Vault KV backend version (1 or 2). '
                  'Autodetect from `vault read /sys/mounts` if not set.')
        )


class Get(KvCommand, ShowOne):
    """
    Retrieves the value from Vault's key-value store at the given key name
    If no key exists with that name, an error is returned. If a key exists with that
    name but has no data, nothing is returned.

      $ hvac-cli kv get secret/foo

    To view the given key name at a specific version in time, specify the "--version"
    flag:

      $ hvac-cli kv get --version=1 secret/foo
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            '--version',
            help='If passed, the value at the version number will be returned. (KvV2 only)',
        )
        parser.add_argument(
            'key',
            help='key to fetch',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return self.dict2columns(kv.read_secret(parsed_args.key, parsed_args.version))


class Delete(KvCommand, Command):
    """
    Deletes the data for the provided version and path in the key-value store
    The versioned data will not be fully removed, but marked as deleted and will no
    longer be returned in normal get requests.

    To delete the latest version of the key "foo":

      $ hvac-cli kv delete secret/foo

    To delete version 3 of key foo:

      $ hvac-cli kv delete --versions=3 secret/foo
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            '--versions',
            help='The comma separate list of version numbers to delete',
        )
        parser.add_argument(
            'key',
            help='key to delete',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        if parsed_args.versions:
            versions = parsed_args.versions.split(',')
        else:
            versions = None
        return kv.delete(parsed_args.key, versions)


class Undelete(KvCommand, Command):
    """
    Undeletes the data for the provided version and path in the key-value store
    This restores the data, allowing it to be returned on get requests.

    To undelete version 3 of key "foo":

      $ hvac-cli kv undelete --versions=3 secret/foo
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            '--versions',
            required=True,
            help='The comma separate list of version numbers to delete',
        )
        parser.add_argument(
            'key',
            help='key to undelete',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return kv.undelete(parsed_args.key, parsed_args.versions.split(','))


class PutOrPatch(KvCommand, ShowOne):
    """
    Writes the data to the given path in the key-value store
    The data can be of any type.

      $ hvac-cli kv put secret/foo bar=baz

    The data can also be consumed from a JSON file on disk. For example:

      $ hvac-cli kv put secret/foo --file=/path/data.json
     """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            '--file',
            help='A JSON object containing the secrets',
        )
        parser.add_argument(
            'key',
            help='key to set',
        )
        parser.add_argument(
            'kvs',
            nargs='*',
            help='k=v secrets that can be repeated. They are ignored if --file is set.',
        )
        return parser

    def parse_kvs(self, kvs):
        r = {}
        for kv in kvs:
            k, v = kv.split('=')
            r[k] = v
        return r

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        if parsed_args.file:
            secrets = json.load(open(parsed_args.file))
        else:
            secrets = self.parse_kvs(parsed_args.kvs)
        self.kv_action(kv, parsed_args, secrets)
        return self.dict2columns(kv.read_secret(parsed_args.key, version=None))


class Put(PutOrPatch):
    """
    Writes the data to the given path in the key-value store
    The data can be of any type.

      $ hvac-cli kv put secret/foo bar=baz

    The data can also be consumed from a JSON file on disk. For example:

      $ hvac-cli kv put secret/foo --file=/path/data.json
     """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument(
            '--cas',
            help=('Specifies to use a Check-And-Set operation. If not set the write will be '
                  'allowed. If set to 0 a write will only be allowed if the key doesn’t '
                  'exist. If the index is non-zero the write will only be allowed if '
                  'the key’s current version matches the version specified in the cas '
                  'parameter. (KvV2 only)'),
        )
        return parser

    def kv_action(self, kv, parsed_args, secrets):
        kv.create_or_update_secret(parsed_args.key, secrets, cas=parsed_args.cas)


class Patch(PutOrPatch):
    """
    Read the data from the given path and merge it with the data provided
    If the existing data is a dictionary named OLD and the data provided
    is a dictionary named NEW, the data stored is the merge of OLD and NEW.
    If a key exists in both NEW and OLD, the one from NEW takes precedence.

      $ hvac-cli kv patch secret/foo bar=baz

    The data can also be consumed from a JSON file on disk. For example:

      $ hvac-cli kv patch secret/foo --file=/path/data.json
     """

    def kv_action(self, kv, parsed_args, secrets):
        kv.patch(parsed_args.key, secrets)


class List(KvCommand, Lister):
    """
    Lists data from Vault's key-value store at the given path.

    List values under the "my-app" folder of the key-value store:

      $ hvac-cli kv list secret/my-app/
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            'path',
            help='path to list',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        r = [[x] for x in kv.list_secrets(parsed_args.path)]
        return (['Keys'], r)


class Dump(KvCommand, Command):
    """Dump all secrets as a JSON object where the keys are the path
    and the values are the secrets. For instance:

    {
      "a/secret/path": { "key1": "value1" },
      "another/secret/path": { "key2": "value2" }
    }
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return kv.dump()


class Load(KvCommand, Command):
    """Load secrets from a JSON object for which the key is the path
    and the value is the secret. For instance:

    {
      "a/secret/path": { "key1": "value1" },
      "another/secret/path": { "key2": "value2" }
    }
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            'path',
            help='path containing secrets in JSON',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return kv.load(parsed_args.path)


class Erase(KvCommand, Command):
    "Erase all secrets"

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return kv.erase()


class MetadataDelete(KvCommand, Command):
    """
    Deletes all versions and metadata for the provided key

      $ hvac-cli kv metadata delete secret/foo
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            'key',
            help='key to delete',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return kv.delete_metadata_and_all_versions(parsed_args.key)


class MetadataGet(KvCommand, ShowOne):
    """
    Retrieves the metadata from Vault's key-value store at the given key name
    If no key exists with that name, an error is returned.

      $ hvac-cli kv metadata get secret/foo

    This command only works with KVv2
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            'key',
            help='get metadata for this key',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        return self.dict2columns(kv.read_secret_metadata(parsed_args.key))


class MetadataPut(KvCommand, ShowOne):
    """
    Create a blank key or update the associated metadata

    Create a key in the key-value store with no data:

      $ hvac-cli kv metadata put secret/foo

    Set a max versions setting on the key:

      $ hvac-cli kv metadata put --max-versions=5 secret/foo

    Require Check-and-Set for this key:

      $ hvac-cli kv metadata put --cas-required=true secret/foo

    This command only works with KVv2
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        self.set_common_options(parser)
        parser.add_argument(
            '--cas-required',
            type=bool,
            default=False,
            help=('If true the key will require the cas parameter to be set on all write '
                  'requests. If false, the backend’s configuration will be used. The '
                  'default is false.')
        )
        parser.add_argument(
            '--max-versions',
            type=int,
            help=('The number of versions to keep. If not set, the backend’s configured '
                  'max version is used.')
        )
        parser.add_argument(
            'key',
            help='set metadata for this key',
        )
        return parser

    def take_action(self, parsed_args):
        kv = kvcli_factory(self.app_args, parsed_args)
        r = kv.update_metadata(parsed_args.key,
                               parsed_args.max_versions,
                               parsed_args.cas_required)
        return self.dict2columns(r)
