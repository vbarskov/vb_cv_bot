/**
 * Контракты DSH-слоя: профиль устанавливает пакет как bundle и получает
 * навыки, а отдельный скрипт добавляет строку stdio MCP с абсолютным путём.
 *
 * Хост DSH здесь не запускается: тесты читают манифесты и вызывают экспорт
 * пакета так же, как это делает загрузчик профиля.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as plugin from '../dsh/lib/index.js';

const root = fileURLToPath(new URL('..', import.meta.url));
const packageName = 'vb-cv-bot';
const skillNames = ['cv-setup', 'cv-queue', 'cv-run'];

function frontmatter(text) {
  const block = text.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1] ?? '';
  return {
    name: block.match(/^name:\s*(.+)$/m)?.[1]?.trim(),
    description: block.match(/^description:\s*(.+)$/m)?.[1]?.trim(),
  };
}

async function manifest(relativePath) {
  return JSON.parse(await readFile(path.join(root, relativePath), 'utf8'));
}

test('корневой пакет объявляет DSH-слой, точку входа и патч', async () => {
  const pkg = await manifest('package.json');
  const manifestFile = await manifest('plugin.json');
  assert.equal(pkg.name, packageName);
  assert.equal(pkg.name, manifestFile.name, 'имя пакета и plugin.json не совпадают');
  assert.equal(pkg.version, manifestFile.version, 'версия пакета и plugin.json не совпадают');
  assert.equal(pkg.private, true);
  assert.equal(pkg.dsh.bundle.patch, './dsh/cordis.patch.yml');
  assert.equal(pkg.main, './dsh/lib/index.js');
  assert.deepEqual(pkg.exports, { '.': './dsh/lib/index.js', './package.json': './package.json' });
  assert.equal(pkg.scripts['dsh:mcp'], 'node dsh/install-mcp.mjs');

  const patch = await readFile(path.join(root, 'dsh/cordis.patch.yml'), 'utf8');
  assert.match(patch, /- insert:/);
  assert.match(patch, /^ {4}- id: vb-cv-bot$/m);
  assert.match(patch, /^ {6}name: vb-cv-bot$/m);
});

test('пакет импортируется по имени так же, как его загружает DSH', async () => {
  const home = await mkdtemp(path.join(tmpdir(), 'dsh-import-'));
  try {
    await mkdir(path.join(home, 'node_modules'), { recursive: true });
    await symlink(root, path.join(home, 'node_modules', packageName), 'dir');
    const script = `const m = await import('${packageName}'); console.log(typeof m.apply, m.name, m.inject.join(','))`;
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], { cwd: home, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), `function ${packageName} skills`);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test('плагин регистрирует три навыка с метаданными из SKILL.md', async () => {
  assert.equal(plugin.name, packageName);
  assert.deepEqual(plugin.inject, ['skills']);
  assert.deepEqual(plugin.SKILL_NAMES, skillNames);

  const providers = [];
  plugin.apply({ skills: { registerProvider(factory) { providers.push(factory()); } } });
  assert.deepEqual(providers.map(provider => provider.name), skillNames);

  for (const [index, provider] of providers.entries()) {
    const [candidate] = await provider.list();
    assert.equal(candidate.name, skillNames[index]);
    assert.equal(candidate.provider, provider.name);
    assert.equal(candidate.provider, skillNames[index]);
    assert.equal(candidate.source, 'bundled');
    assert.equal(candidate.rank, 600);
    assert.equal(candidate.invocation.modelInvocable, true);
    assert.equal(candidate.invocation.userInvocable, true);

    const text = await readFile(candidate.path, 'utf8');
    assert.deepEqual(frontmatter(text), { name: candidate.name, description: candidate.description });
    assert.equal(candidate.resourceBase.path, path.join(root, 'skills', candidate.name, '/'));

    const loaded = await provider.get();
    assert.equal(loaded.name, candidate.name);
    assert.equal(loaded.description, candidate.description);
    assert.ok(loaded.content.trim().length > 0);
    assert.equal(loaded.content.trimStart().startsWith('---'), false);
  }
});

test('install-mcp.mjs идемпотентно добавляет MCP-строку и не трогает чужие строки', async () => {
  const home = await mkdtemp(path.join(tmpdir(), 'dsh-home-'));
  try {
    const profile = path.join(home, 'profiles', 'desktop');
    await mkdir(profile, { recursive: true });
    const target = path.join(profile, 'cordis.patch.yml');
    await writeFile(target, '- id: ui-chat\n  name: "@deepseek-ai/dsh-client-ui-chat"\n');

    const run = (...extra) => spawnSync(process.execPath, [path.join(root, 'dsh/install-mcp.mjs'), ...extra], {
      env: { ...process.env, DSH_HOME: home },
      encoding: 'utf8',
    });
    assert.equal(run().status, 0);
    assert.equal(run().status, 0);

    const text = await readFile(target, 'utf8');
    assert.equal(text.split('# vb-cv-bot: начало').length - 1, 1);
    assert.match(text, /serverName: vb-cv-bot/);
    assert.match(text, /command: python3/);
    assert.match(text, /\n {10}- ".*scripts\/mcp_server\.py"\n/);
    assert.match(text, /- id: ui-chat/);

    const printed = run('--print');
    assert.equal(printed.status, 0);
    assert.match(printed.stdout, /serverName: vb-cv-bot/);
    assert.equal(await readFile(target, 'utf8'), text);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test('install-mcp.mjs заменяет пустой список профиля', async () => {
  const home = await mkdtemp(path.join(tmpdir(), 'dsh-home-'));
  try {
    const profile = path.join(home, 'profiles', 'desktop');
    await mkdir(profile, { recursive: true });
    const target = path.join(profile, 'cordis.patch.yml');
    await writeFile(target, '[]\n');
    const result = spawnSync(process.execPath, [path.join(root, 'dsh/install-mcp.mjs')], {
      env: { ...process.env, DSH_HOME: home },
      encoding: 'utf8',
    });
    assert.equal(result.status, 0);
    assert.match(await readFile(target, 'utf8'), /^- insert:$/m);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});
