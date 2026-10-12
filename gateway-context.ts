// gateway-context.ts — плагин Kilo Code / OpenCode: добавляет к каждому запросу в LLM-шлюз
// заголовки контекста X-Repo, X-Branch, X-Jira-Key, X-Session-Id, X-Tool.
//
// Установка (глобально): %USERPROFILE%\.config\kilo\plugin\gateway-context.ts
// Отладочный лог: %LOCALAPPDATA%\mitm-llm\plugin.log
//
// Как определяется репозиторий сессии (в порядке приоритета):
//   1) последний файл или папка, с которыми работал агент в этой сессии
//      (пути в аргументах read/edit/write, `cd X` и `git -C X` в командах bash, cwd shell-команд,
//      папка, в которую агент склонировал репозиторий через `git clone`) — где угодно на диске;
//   2) папка сессии из событий или SDK-клиента, если это репозиторий;
//   3) рабочая папка Kilo, если это репозиторий;
//   4) если рабочая папка содержит ровно один репозиторий (на 1–2 уровня ниже) — он;
//   5) GATEWAY_CTX_REPO_DIR, если задана.
// Если в рабочей папке несколько репозиториев, до первого обращения агента к файлам
// репозиторий неизвестен — заголовки X-Repo/X-Branch будут пустыми, причина пишется в лог.
//
// Переменные окружения (необязательно):
//   GIT_EXE              — полный путь к git.exe, если git не находится в PATH процесса Kilo
//   GATEWAY_CTX_REPO_DIR — принудительно использовать эту папку (для отладки)
//   GATEWAY_CTX_DEBUG    — "1": писать в лог подробности по каждому запросу и инструменту

import { execFileSync } from "node:child_process"
import { appendFileSync, existsSync, mkdirSync, readdirSync } from "node:fs"
import { basename, dirname, isAbsolute, join, resolve } from "node:path"

const TOOL = "kilocode" // для OpenCode поставьте "opencode"
const GIT = process.env.GIT_EXE || "git"
const DEBUG = process.env.GATEWAY_CTX_DEBUG === "1"
const FORCED_DIR = process.env.GATEWAY_CTX_REPO_DIR || ""

const LOG_DIR = join(process.env.LOCALAPPDATA ?? process.env.HOME ?? ".", "mitm-llm")
function dbg(msg: string) {
  try {
    mkdirSync(LOG_DIR, { recursive: true })
    appendFileSync(join(LOG_DIR, "plugin.log"), `${new Date().toISOString()} ${msg}\n`)
  } catch {
    // отладочный лог не должен ломать агента
  }
}

// Убираем из URL репозитория логин/токен и .git, чтобы не отправлять секреты в заголовке
export function normalizeRepo(url: string): string {
  return url.trim().replace(/^(https?:\/\/)[^@/]+@/, "$1").replace(/\.git$/, "")
}

export function jiraKeyFromBranch(branch: string): string {
  return branch.match(/[A-Z][A-Z0-9]+-\d+/)?.[0] ?? ""
}

function git(cwd: string, args: string[]): { out: string; err?: string } {
  try {
    const out = execFileSync(GIT, args, {
      cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], windowsHide: true, timeout: 5000,
    })
    return { out: out.trim() }
  } catch (e: any) {
    const err = (e?.stderr?.toString?.() || e?.message || String(e)).trim().split("\n")[0]
    return { out: "", err }
  }
}

// Корень репозитория: сама папка (или папка файла) либо ближайшая родительская с .git
export function findRepoRoot(start: string | undefined): string | undefined {
  if (!start) return undefined
  let cur = resolve(start)
  for (let i = 0; i < 64; i++) {
    if (existsSync(join(cur, ".git"))) return cur
    const up = dirname(cur)
    if (up === cur) return undefined
    cur = up
  }
  return undefined
}

const SKIP_DIRS = new Set(["node_modules", ".git", ".venv", "venv", "build", "dist", "out", "target"])

// Репозитории внутри папки (1–2 уровня), если сама папка репозиторием не является
export function findSubRepos(dir: string, depth = 2): string[] {
  const found: string[] = []
  const walk = (d: string, level: number) => {
    let entries: any[] = []
    try { entries = readdirSync(d, { withFileTypes: true }) } catch { return }
    for (const e of entries) {
      if (!e.isDirectory() || SKIP_DIRS.has(e.name) || e.name.startsWith(".")) continue
      const p = join(d, e.name)
      if (existsSync(join(p, ".git"))) found.push(p)
      else if (level < depth) walk(p, level + 1)
    }
  }
  walk(dir, 1)
  return found
}

// Пути, которые агент упоминает в командах shell: `cd X`, `pushd X`, `git -C X`
// Куда `git clone` положит репозиторий: явная папка или имя репозитория из URL.
// Относительный путь считается от последнего `cd` перед clone, иначе от рабочей папки сессии.
export function cloneTargets(cmd: string, cwd: string): string[] {
  const out: string[] = []
  let cur = cwd
  for (const part of cmd.split(/&&|;|\|\||\n/)) {
    const cd = part.match(/^\s*(?:cd|pushd|Set-Location)\s+("([^"]+)"|'([^']+)'|(\S+))/i)
    if (cd) { const d = cd[2] ?? cd[3] ?? cd[4]; cur = isAbsolute(d) ? d : resolve(cur, d); continue }
    const m = part.match(/\bgit\s+clone\b(.*)/i)
    if (!m) continue
    const toks = (m[1].match(/"[^"]*"|'[^']*'|\S+/g) ?? []).map((t) => t.replace(/^["']|["']$/g, ""))
    const positional: string[] = []
    for (let i = 0; i < toks.length; i++) {
      const t = toks[i]
      if (t.startsWith("-")) {
        if (/^(-b|--branch|-o|--origin|--depth|-c|--config|--reference|--separate-git-dir|-j|--jobs)$/.test(t)) i++
        continue
      }
      positional.push(t)
    }
    if (!positional.length) continue
    const url = positional[0]
    const dir = positional[1] ?? basename(url.replace(/[\\/]+$/, "")).replace(/\.git$/, "").replace(/^.*:/, "")
    out.push(isAbsolute(dir) ? dir : resolve(cur, dir))
  }
  return out
}

export function pathsFromCommand(cmd: string): string[] {
  const out: string[] = []
  const re = /(?:\bcd|\bpushd|\bSet-Location|\bgit\s+-C)\s+("([^"]+)"|'([^']+)'|([^\s;&|]+))/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(cmd))) out.push(m[2] ?? m[3] ?? m[4])
  return out
}

function repoContext(root: string | undefined) {
  if (!root) return { repo: "", branch: "", why: "репозиторий не определён" }
  const remotes = git(root, ["remote"])
  if (remotes.err) return { repo: "", branch: "", why: `git не запустился (${GIT}) в ${root}: ${remotes.err}` }
  const names = remotes.out.split(/\s+/).filter(Boolean)
  const remote = names.includes("origin") ? "origin" : names[0]
  const url = remote ? git(root, ["remote", "get-url", remote]).out : ""
  let branch = git(root, ["branch", "--show-current"]).out
  if (!branch) branch = git(root, ["rev-parse", "--short", "HEAD"]).out // detached HEAD
  return { repo: normalizeRepo(url), branch, why: remote ? "" : "у репозитория нет remote" }
}

const PATH_ARGS = ["filePath", "file_path", "path", "workdir", "cwd", "directory", "dir"]

export const GatewayContext = async (ctx: any) => {
  // worktree="/" означает «рабочая папка не в git» — такой worktree игнорируем
  const worktree = ctx?.worktree && ctx.worktree !== "/" ? ctx.worktree : undefined
  const baseDir: string = FORCED_DIR || worktree || ctx?.directory || process.cwd()

  const sessionRoots = new Map<string, string>()   // сессия → корень репозитория
  const sessionDirs = new Map<string, string>()    // сессия → рабочая папка сессии
  const pending = new Map<string, Set<string>>()     // сессия → пути, которых ещё нет (clone в процессе)
  let loggedInput = false
  let loggedTools = 0

  const subReposOf = (dir: string) => findSubRepos(dir)
  const startSubs = findRepoRoot(baseDir) ? [] : subReposOf(baseDir)
  dbg(`loaded: directory=${ctx?.directory} worktree=${ctx?.worktree} cwd=${process.cwd()} ` +
      `forced=${FORCED_DIR || "-"} baseRoot=${findRepoRoot(baseDir) ?? "-"} ` +
      `subRepos=${startSubs.length ? startSubs.map((p) => basename(p)).join(",") : "-"}`)

  const dirOf = (sid: string) => sessionDirs.get(sid) || baseDir

  const remember = (sessionID: unknown, path: unknown, source: string) => {
    if (!sessionID || typeof path !== "string" || !path.trim()) return
    const sid = String(sessionID)
    const abs = isAbsolute(path) ? path : resolve(dirOf(sid), path)
    const root = findRepoRoot(abs)
    if (root && sessionRoots.get(sid) !== root) {
      sessionRoots.set(sid, root)
      dbg(`session ${sid} → ${root} (источник: ${source})`)
    }
  }

  // Путь, которого ещё нет на диске (например, цель git clone): проверим после выполнения
  const later = (sessionID: unknown, path: string) => {
    if (!sessionID) return
    const sid = String(sessionID)
    if (!pending.has(sid)) pending.set(sid, new Set())
    pending.get(sid)!.add(path)
  }
  const retryPending = (sid: string, source: string) => {
    const set = pending.get(sid)
    if (!set) return
    for (const p of [...set]) {
      if (findRepoRoot(p)) { remember(sid, p, source); set.delete(p) }
    }
  }

  const rememberDir = (sessionID: unknown, dir: unknown, source: string) => {
    if (!sessionID || typeof dir !== "string" || !dir) return
    sessionDirs.set(String(sessionID), dir)
    if (findRepoRoot(dir)) remember(sessionID, dir, source)
  }

  // Спросить у Kilo/OpenCode папку сессии через SDK-клиент (с таймаутом)
  const lookupSession = async (sid: string) => {
    try {
      const call = ctx?.client?.session?.get?.({ path: { id: sid } })
      if (!call) return
      const res: any = await Promise.race([call, new Promise((r) => setTimeout(() => r(undefined), 1500))])
      const info = res?.data ?? res
      rememberDir(sid, info?.directory ?? info?.path?.cwd, "client.session.get")
    } catch (e: any) {
      dbg(`client.session.get не сработал: ${e?.message ?? e}`)
    }
  }

  const resolveRoot = (sid: string): { root?: string; why?: string } => {
    const known = sessionRoots.get(sid)
    if (known) return { root: known }
    const dir = dirOf(sid)
    const direct = findRepoRoot(dir)
    if (direct) return { root: direct }
    const subs = subReposOf(dir)
    if (subs.length === 1) return { root: subs[0] }
    if (subs.length > 1) {
      return { why: `в ${dir} несколько репозиториев (${subs.map((p) => basename(p)).join(", ")}), ` +
                    `агент ещё не обращался к их файлам` }
    }
    return { why: `в ${dir} и выше нет .git, и внутри репозиториев не найдено` }
  }

  return {
    event: async ({ event }: any) => {
      try {
        const info = event?.properties?.info
        if (info?.id && info?.directory) rememberDir(info.id, info.directory, event.type)
      } catch { /* не мешаем агенту */ }
    },

    "tool.execute.before": async (input: any, output: any) => {
      try {
        const sid = input?.sessionID
        const args = output?.args ?? {}
        if (DEBUG || loggedTools < 5) {
          loggedTools++
          dbg(`tool ${input?.tool}: args keys=${Object.keys(args).join(",")}`)
        }
        for (const k of PATH_ARGS) {
          if (typeof args[k] === "string" && args[k]) { remember(sid, args[k], `tool ${input?.tool}.${k}`); break }
        }
        if (typeof args.command === "string") {
          const cwd = typeof args.workdir === "string" ? args.workdir : dirOf(String(sid))
          for (const p of pathsFromCommand(args.command)) remember(sid, p, `tool ${input?.tool}: команда`)
          for (const p of cloneTargets(args.command, cwd)) later(sid, p)
        }
      } catch { /* не мешаем агенту */ }
    },

    // после выполнения инструмента: появился ли склонированный репозиторий
    "tool.execute.after": async (input: any) => {
      try { if (input?.sessionID) retryPending(String(input.sessionID), `git clone (tool ${input?.tool})`) } catch { /* ignore */ }
    },

    "shell.env": async (input: any) => {
      try { remember(input?.sessionID, input?.cwd, "shell.env") } catch { /* ignore */ }
    },

    "chat.headers": async (input: any, output: any) => {
      const sid = String(input?.sessionID ?? "")
      if (!loggedInput) {
        dbg(`chat.headers input keys: ${Object.keys(input ?? {}).join(", ")}`)
        loggedInput = true
      }
      if (sid) retryPending(sid, "git clone")
      if (sid && !sessionDirs.has(sid) && !sessionRoots.has(sid)) await lookupSession(sid)
      const r = resolveRoot(sid)
      const c = repoContext(r.root)
      const why = r.why || c.why
      const headers: Record<string, string> = {
        "X-Repo": c.repo,
        "X-Branch": c.branch,
        "X-Jira-Key": jiraKeyFromBranch(c.branch),
        "X-Session-Id": sid,
        "X-Tool": TOOL,
      }
      output.headers = { ...(output.headers ?? {}), ...headers }
      if (DEBUG || why || !c.branch) {
        dbg(`chat.headers: session=${sid} root=${r.root ?? "-"} repo=${c.repo || "-"} ` +
            `branch=${c.branch || "-"} jira=${headers["X-Jira-Key"] || "-"}${why ? " ПРИЧИНА: " + why : ""}`)
      }
    },
  }
}
