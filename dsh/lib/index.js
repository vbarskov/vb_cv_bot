/**
 * DSH-слой VB CV Bot.
 *
 * Регистрирует три навыка плагина (`cv-setup`, `cv-queue`, `cv-run`) как
 * bundled-провайдеры `ctx.skills`, чтобы они были доступны в DeepSeek Harness
 * в любом рабочем каталоге. Инструкции читаются из тех же
 * `skills/<имя>/SKILL.md`, что использует Codex, поэтому текст один.
 *
 * Контракт реестра DSH: у провайдера есть `name`, и каждый кандидат из
 * `list()` обязан вернуть это же значение в поле `provider`. Иначе реестр
 * отклоняет каталог навыков на каждом ходу — и падает каждое сообщение.
 * Поэтому каждый навык обслуживается одноимённым провайдером.
 *
 * Пакет импортирует только встроенные модули Node.js: так установка из
 * GitHub или `link:` работает без зависимостей в профиле DSH.
 */
import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

/** Навыки плагина в порядке каталога: сначала подключение. */
export const SKILL_NAMES = ['cv-setup', 'cv-queue', 'cv-run'];

/** Ранг bundled-провайдера из `@deepseek-ai/dsh-skill`. */
const BUNDLED_SKILL_RANK = 600;

/** Доступ из модели и из пользовательского `/`. */
const INVOCATION = { modelInvocable: true, userInvocable: true };

/** YAML-описание навыка в начале `SKILL.md`. */
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

/** Публичная грамматика имён навыков. */
const SKILL_NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** URL файла навыка. */
function skillFile(name) {
  return new URL(`../../skills/${name}/SKILL.md`, import.meta.url);
}

/** Абсолютный путь каталога навыка: относительно него работают `references/…`. */
function skillDirectory(name) {
  return fileURLToPath(new URL(`../../skills/${name}/`, import.meta.url));
}

/** Убрать YAML-описание, оставив тело инструкций. */
function stripFrontmatter(text) {
  return text.replace(FRONTMATTER, '');
}

/** Разобрать обязательные поля YAML-описания. */
function parseFrontmatter(text, name) {
  const block = text.match(FRONTMATTER)?.[1];
  if (!block) throw new Error(`vb-cv-bot: у навыка ${name} нет YAML-описания`);
  const frontName = block.match(/^name:\s*(.+)$/m)?.[1]?.trim();
  const description = block.match(/^description:\s*(.+)$/m)?.[1]?.trim();
  if (frontName !== name) throw new Error(`vb-cv-bot: имя навыка ${frontName ?? '(нет)'} не совпадает с каталогом ${name}`);
  if (!description) throw new Error(`vb-cv-bot: у навыка ${name} нет описания`);
  return { description };
}

/** Прочитать файл навыка синхронно: так ошибка установки видна при монтировании. */
function readSkillFile(name) {
  try {
    return readFileSync(skillFile(name), 'utf8');
  } catch (error) {
    throw new Error(`vb-cv-bot: не удалось прочитать навык ${name}: ${error.message}`, { cause: error });
  }
}

/** Проверить все навыки до регистрации провайдеров. */
function validateSkills() {
  for (const name of SKILL_NAMES) {
    if (!SKILL_NAME_PATTERN.test(name)) throw new Error(`vb-cv-bot: недопустимое имя навыка ${name}`);
    const text = readSkillFile(name);
    parseFrontmatter(text, name);
    if (!stripFrontmatter(text).trim()) throw new Error(`vb-cv-bot: навык ${name} не содержит инструкций`);
  }
}

/** Загрузить навык целиком: описание, путь и тело инструкций. */
async function loadSkill(name) {
  const file = skillFile(name);
  const text = await readFile(file, 'utf8');
  const { description } = parseFrontmatter(text, name);
  const body = stripFrontmatter(text);
  if (!body.trim()) throw new Error(`vb-cv-bot: навык ${name} не содержит инструкций`);
  return {
    name,
    description,
    invocation: INVOCATION,
    // Реестр DSH требует, чтобы кандидат указывал имя своего провайдера.
    provider: name,
    source: 'bundled',
    resourceBase: { kind: 'directory', path: skillDirectory(name) },
    path: fileURLToPath(file),
    content: body,
  };
}

/** Провайдер одного навыка: реестр DSH вызывает `list` и `get`. */
function skillProvider(name) {
  return {
    name,
    async list() {
      const { content: _content, ...summary } = await loadSkill(name);
      return [{ ...summary, rank: BUNDLED_SKILL_RANK, locator: skillFile(name) }];
    },
    async get() {
      return loadSkill(name);
    },
  };
}

/** Имя Cordis-плагина в списке плагинов профиля. */
export const name = 'vb-cv-bot';

/** Реестр навыков, который заполняет этот плагин. */
export const inject = ['skills'];

/**
 * Проверить инструкции и зарегистрировать провайдеров навыков.
 * @param {import('@deepseek-ai/cordis').Context} ctx - контекст с `ctx.skills`.
 */
export function apply(ctx) {
  validateSkills();
  for (const skillName of SKILL_NAMES) ctx.skills.registerProvider(() => skillProvider(skillName));
}
