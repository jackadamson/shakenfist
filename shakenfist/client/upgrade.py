# Copyright 2020 Michael Still

import etcd3
import json

from shakenfist import db

# Very simple data upgrader


def main():
    client = etcd3.client()

    versions = {}
    for node in db.get_nodes():
        versions.setdefault(node.get('version', 'unknown'), 0)
        versions[node.get('version', 'unknown')] += 1

    print('Deployed versions:')
    for version in sorted(versions):
        print(' - %s: %s' % (version, versions[version]))
    print()

    min_version = None
    if 'unknown' in versions:
        min_version = '0.2'
    else:
        min_version = sorted(versions)[0]
    print('Minimum version is %s' % min_version)

    elems = min_version.split('.')
    major = int(elems[0])
    minor = int(elems[1])

    if major == 0:
        if minor == 2:
            # We probably need to cleanup excess network mesh events
            for event, metadata in client.get_prefix('/sf/event/network'):
                event = json.loads(event)
                if event['operation'] in ['ensure mesh', 'discover mesh']:
                    print('--> Removing overly verbose network event %s'
                          % metadata.key)
                    client.delete(metadata.key)


if __name__ == '__main__':
    main()