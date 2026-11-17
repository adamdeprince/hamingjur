#!/usr/bin/env python3
"""Deploy the generated HTML to R2 and its dedicated Worker custom domain.

Credentials stay in memory and reach curl through stdin, never command arguments.
Use --socks-proxy 127.0.0.1:1085 with an SSH dynamic forward when the token is
restricted to the forwarding host's network. No credentials are copied remotely.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


class CloudflareError(RuntimeError):
    """Retain an unsuccessful API response for explicit error handling."""

    def __init__(self, status, response):
        self.status = status
        super().__init__(f'Cloudflare HTTP {status}: {response.get("errors", response)}')


class Cloudflare:
    """Call Cloudflare through curl, optionally using SOCKS with remote DNS."""

    def __init__(self, token, proxy=None):
        if not token or any(c.isspace() for c in token):
            raise ValueError('Expected one Cloudflare API token')
        self.token, self.proxy = token, proxy

    def request(self, method, path, data=None, file=None, forms=(), content_type=None):
        config = ['url = ' + json.dumps('https://api.cloudflare.com/client/v4' + path),
                  'header = ' + json.dumps('Authorization: Bearer ' + self.token)]
        if data is not None:
            content_type = 'application/json'
        if content_type:
            config.append('header = ' + json.dumps('Content-Type: ' + content_type))
        command = ['curl', '--silent', '--show-error', '--connect-timeout', '20',
                   '--max-time', '120', '--request', method, '--config', '-',
                   '--write-out', '\n%{http_code}']
        if self.proxy:
            command.extend(['--socks5-hostname', self.proxy])
        if data is not None:
            command.extend(['--data-binary', json.dumps(data)])
        if file is not None:
            command.extend(['--data-binary', '@' + str(file)])
        for form in forms:
            command.extend(['--form', form])
        result = subprocess.run(command, input=('\n'.join(config) + '\n').encode(),
                                capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace').replace(self.token, '[redacted]'))
        body, status = result.stdout.rsplit(b'\n', 1)
        response = json.loads(body) if body else {'success': 200 <= int(status) < 300}
        if not response.get('success'):
            raise CloudflareError(int(status), response)
        return response.get('result')

    def get(self, path):
        return self.request('GET', path)


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, help='token file, instead of CLOUDFLARE_API_TOKEN')
    parser.add_argument('--socks-proxy', help='SSH SOCKS listener, e.g. 127.0.0.1:1085')
    args = parser.parse_args()
    token = (args.token_file.expanduser().read_text().strip().strip('\"\x27')
             if args.token_file else os.environ.get('CLOUDFLARE_API_TOKEN', ''))
    if not token:
        parser.error('Supply --token-file or CLOUDFLARE_API_TOKEN')
    config = json.loads((project / 'wrangler.jsonc').read_text())
    account, worker = config['account_id'], config['name']
    domain = config['routes'][0]['pattern']
    bucket = config['r2_buckets'][0]['bucket_name']
    html = project / 'dist' / 'index.html'
    if not html.is_file():
        parser.error('Run build.py before deployment')
    client = Cloudflare(token, args.socks_proxy)
    zones = client.get('/zones?name=goblinreactor.com')
    if len(zones) != 1 or zones[0]['account']['id'] != account:
        raise RuntimeError('The DNS zone must belong to the configured Cloudflare account')
    zone = zones[0]['id']
    domains = client.get(f'/accounts/{account}/workers/domains')
    existing_domain = next((d for d in domains if d['hostname'] == domain), None)
    if existing_domain and existing_domain['service'] != worker:
        raise RuntimeError('The hostname already belongs to a different Worker')
    dns = client.get(f'/zones/{zone}/dns_records?name={domain}')
    if dns and not existing_domain:
        raise RuntimeError('The hostname already has DNS records; refusing to replace another origin')
    try:
        current = client.get(f'/accounts/{account}/workers/scripts/{worker}/settings')
    except CloudflareError as error:
        if error.status != 404:
            raise
        current = None
    if current and not any(b.get('bucket_name') == bucket for b in current.get('bindings', [])):
        raise RuntimeError('The Worker name already belongs to a different deployment')
    try:
        client.get(f'/accounts/{account}/r2/buckets/{bucket}')
    except CloudflareError as error:
        if error.status != 404:
            raise
        client.request('POST', f'/accounts/{account}/r2/buckets', data={'name': bucket})
        print('Created R2 bucket:', bucket, flush=True)
    client.request('PUT', f'/accounts/{account}/r2/buckets/{bucket}/objects/index.html',
                   file=html, content_type='text/html; charset=utf-8')
    print('Uploaded R2 object: index.html', flush=True)
    metadata = {'main_module': config['main'], 'compatibility_date': config['compatibility_date'],
                'bindings': [{'type': 'r2_bucket', 'name': b['binding'],
                              'bucket_name': b['bucket_name']} for b in config['r2_buckets']]
                + [{'type': 'plain_text', 'name': key, 'text': value}
                   for key, value in config.get('vars', {}).items()]}
    with tempfile.TemporaryDirectory(prefix='hamingjur-worker-') as directory:
        metadata_file = Path(directory) / 'metadata.json'
        metadata_file.write_text(json.dumps(metadata))
        result = client.request('PUT', f'/accounts/{account}/workers/scripts/{worker}',
                                forms=[f'metadata=@{metadata_file};type=application/json',
                                       f'{config["main"]}=@{project / config["main"]};'
                                       'type=application/javascript+module'])
    print('Deployed Worker:', worker, flush=True)
    client.request('POST', f'/accounts/{account}/workers/scripts/{worker}/subdomain',
                   data={'enabled': False, 'previews_enabled': False})
    result = client.request('PUT', f'/accounts/{account}/workers/domains',
                            data={'hostname': domain, 'service': worker,
                                  'zone_id': zone, 'enabled': True, 'previews_enabled': False})
    print(json.dumps({'homepage': config['vars']['SITE_URL'], 'domain_id': result['id'],
                      'bucket': bucket, 'worker': worker}), flush=True)


if __name__ == '__main__':
    main()
