import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.mjs';

function environment(body = 'Hamingjur', found = true) {
  const calls = [];
  const object = () => found ? {
    httpEtag: '"version-1"',
    body: new Response(body).body,
    writeHttpMetadata(headers) { headers.set('Content-Type', 'application/octet-stream'); }
  } : null;
  return {
    SITE_URL: 'https://hamingjur.goblinreactor.com', calls,
    DOCS: {
      async get(key) { calls.push(['get', key]); return object(); },
      async head(key) { calls.push(['head', key]); return object(); }
    }
  };
}

test('root serves the complete R2 stream as HTML, including content beyond 64 KiB', async () => {
  const body = '<!doctype html>' + 'Hamingjur αβγ\n'.repeat(20000);
  const env = environment(body);
  const response = await worker.fetch(new Request(env.SITE_URL + '/'), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Content-Type'), 'text/html; charset=utf-8');
  assert.equal(await response.text(), body);
  assert.deepEqual(env.calls, [['get', 'index.html']]);
});

test('HEAD reads only R2 metadata and returns no body', async () => {
  const env = environment();
  const response = await worker.fetch(new Request(env.SITE_URL + '/', { method: 'HEAD' }), env);
  assert.deepEqual(env.calls, [['head', 'index.html']]);
  assert.equal(response.headers.get('ETag'), '"version-1"');
  assert.equal(await response.text(), '');
});

test('conditional GET returns 304 for an unchanged object', async () => {
  const env = environment();
  const response = await worker.fetch(new Request(env.SITE_URL, {
    headers: { 'If-None-Match': '"version-1"' }
  }), env);
  assert.equal(response.status, 304);
  assert.equal(await response.text(), '');
});

test('index.html redirects to the canonical homepage', async () => {
  const env = environment();
  const response = await worker.fetch(new Request(env.SITE_URL + '/index.html'), env);
  assert.equal(response.status, 308);
  assert.equal(response.headers.get('Location'), env.SITE_URL + '/');
  assert.deepEqual(env.calls, []);
});

test('missing objects return 404 for GET and empty HEAD', async () => {
  for (const method of ['GET', 'HEAD']) {
    const env = environment('', false);
    const response = await worker.fetch(new Request(env.SITE_URL + '/missing', { method }), env);
    assert.equal(response.status, 404);
    assert.equal(await response.text(), method === 'HEAD' ? '' : 'Not found');
  }
});

test('write methods cannot mutate the public bucket', async () => {
  const env = environment();
  const response = await worker.fetch(new Request(env.SITE_URL, { method: 'POST' }), env);
  assert.equal(response.status, 405);
  assert.equal(response.headers.get('Allow'), 'GET, HEAD');
  assert.deepEqual(env.calls, []);
});

test('invalid URL encoding returns 400 without touching R2', async () => {
  const env = environment();
  const response = await worker.fetch(new Request(env.SITE_URL + '/%FF'), env);
  assert.equal(response.status, 400);
  assert.deepEqual(env.calls, []);
});
