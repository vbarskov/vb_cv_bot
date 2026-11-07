#!/usr/bin/env node
/**
 * Добавить MCP-сервер VB CV Bot в профиль DeepSeek Harness.
 *
 * Навыки подключаются строкой из `dsh/cordis.patch.yml` при установке пакета.
 * MCP-сервер запускается отдельным процессом, и его путь зависит от машины,
 * поэтому строка с абсолютным путём дописывается сюда.
 *
 * Скрипт идемпотентен: повторный запуск обновляет свой блок, а не дублирует
 * его. Пользовательские строки файла не изменяются.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Корень пакета: каталог `dsh/` на уровень выше скрипта. */
const PACKAGE_ROOT = fileURLToPath(new URL('..', import.meta.url));

/** Готовый к запуску stdio MCP-сервер этого пакета (только стандартная библиотека Python). */
const SERVER_PATH = path.join(PACKAGE_ROOT, 'scripts', 'mcp_server.py');

/** Маркеры собственного блока: по ним скрипт находит его при повторном запуске. */
const START = '# vb-cv-bot: начало (сгенерировано dsh/install-mcp.mjs)';
const END = '# vb-cv-bot: конец';

/** Разобрать `--имя значение` из аргументов командной строки. */
function option(args, flag, fallback) {
  const index = args.indexOf(flag);
  return index < 0 ? fallback : args[index + 1];
}

/** Текст блока MCP для патча профиля. */
function blockText() {
  return [
    START,
    '- insert:',
    '    - id: mcp-vb-cv-bot',
    '      name: "@deepseek-ai/dsh-mcp-client"',
    '      config:',
    '        serverName: vb-cv-bot',
    '        transport: stdio',
    '        command: python3',
    '        args:',
    '          - ' + JSON.stringify(SERVER_PATH),
    '        toolCallTimeoutMs: 120000',
    END,
  ].join('\n');
}

/** Убрать строку пустого списка: иначе наш список окажется вторым YAML-документом. */
function withoutEmptyList(text) {
  return text.replace(/^[ \t]*\[\][ \t]*\r?\n?/gm, '');
}

/** Вставить блок в конец патча, заменив прежний такой же блок или пустой список. */
function applyBlock(text, block) {
  const start = text.indexOf(START);
  const end = text.indexOf(END);
  if (start >= 0 && end > start) {
    return withoutEmptyList(text.slice(0, start)) + block + withoutEmptyList(text.slice(end + END.length));
  }
  const cleaned = withoutEmptyList(text);
  return (cleaned.trimEnd() ? cleaned.trimEnd() + '\n' : '') + block + '\n';
}

const args = process.argv.slice(2);
const profile = option(args, '--profile', 'desktop');
const home = process.env.DSH_HOME || path.join(homedir(), '.dsh');
const target = path.join(home, 'profiles', profile, 'cordis.patch.yml');
const block = blockText();

if (args.includes('--print')) {
  process.stdout.write(block + '\n');
} else {
  await mkdir(path.dirname(target), { recursive: true });
  let current = '';
  try {
    current = readFileSync(target, 'utf8');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  writeFileSync(target, applyBlock(current, block));
  process.stdout.write(`MCP vb-cv-bot добавлен в ${target}.\n`);
  process.stdout.write('Начните новый чат DSH: инструменты mcp__vb-cv-bot__* появятся после перезапуска профиля.\n');
}
