/**
 * IndexedDB, the only place this UI keeps anything.
 *
 * Three stores: the settings a person set (the root URL they last used),
 * every environment they have opened, and every call they have made against
 * one. A transcript and a saved final state outlive the socket that produced
 * them, which is what makes "close the tab" a recoverable event rather than a
 * lost session.
 *
 * Written against the raw IndexedDB API on purpose: a wrapper library would be
 * a larger dependency than the code it replaces, and the build is one file.
 */

const DB_NAME = "openenv-ui"
const DB_VERSION = 1

export type EnvStatus = "connecting" | "live" | "closed" | "expired" | "failed"

export type EnvRecord = {
  id: string
  root: string
  label: string
  envName: string | null
  resetArgs: Record<string, unknown>
  createdAt: number
  closedAt: number | null
  status: EnvStatus
  /** Why a `failed` or `expired` environment ended, in the words shown to the person. */
  note: string | null
  /** `state` as it was when the environment was closed, saved by default. */
  finalState: Record<string, unknown> | null
}

export type CallKind = "reset" | "tool" | "step" | "state"

export type CallRecord = {
  /** `envId:seq`, so that writing the same call twice replaces it rather than doubling it. */
  id: string
  envId: string
  seq: number
  ts: number
  kind: CallKind
  /** The tool name for a tool call; the frame type otherwise. */
  name: string
  args: Record<string, unknown>
  ok: boolean
  result: unknown
  error: { code?: string; message: string; details?: unknown } | null
  durationMs: number
}

let handle: Promise<IDBDatabase> | null = null

function open(): Promise<IDBDatabase> {
  if (handle) return handle
  handle = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains("settings")) {
        db.createObjectStore("settings")
      }
      if (!db.objectStoreNames.contains("envs")) {
        db.createObjectStore("envs", { keyPath: "id" })
      }
      if (!db.objectStoreNames.contains("calls")) {
        // The key is the record's own `id`, which every caller supplies.
        // `autoIncrement` is kept so that a row written by an older build,
        // which supplied none, still reads back.
        const calls = db.createObjectStore("calls", { keyPath: "id", autoIncrement: true })
        calls.createIndex("envId", "envId", { unique: false })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  return handle
}

function run<T>(
  store: string,
  mode: IDBTransactionMode,
  body: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const transaction = db.transaction(store, mode)
        const request = body(transaction.objectStore(store))
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      }),
  )
}

export const settings = {
  get: <T>(key: string): Promise<T | undefined> =>
    run<T | undefined>("settings", "readonly", (store) => store.get(key)),
  set: (key: string, value: unknown): Promise<unknown> =>
    run("settings", "readwrite", (store) => store.put(value, key)),
}

export const envs = {
  all: (): Promise<EnvRecord[]> =>
    run<EnvRecord[]>("envs", "readonly", (store) => store.getAll() as IDBRequest<EnvRecord[]>),
  put: (record: EnvRecord): Promise<unknown> =>
    run("envs", "readwrite", (store) => store.put(record)),
  remove: (id: string): Promise<unknown> =>
    run("envs", "readwrite", (store) => store.delete(id)),
}

export const calls = {
  // `put` and not `add`: writing a call twice is a replace, not a second row.
  put: (record: CallRecord): Promise<unknown> =>
    run("calls", "readwrite", (store) => store.put(record)),
  forEnv: (envId: string): Promise<CallRecord[]> =>
    open().then(
      (db) =>
        new Promise((resolve, reject) => {
          const transaction = db.transaction("calls", "readonly")
          const index = transaction.objectStore("calls").index("envId")
          const request = index.getAll(IDBKeyRange.only(envId))
          request.onsuccess = () => resolve(request.result as CallRecord[])
          request.onerror = () => reject(request.error)
        }),
    ),
  removeForEnv: (envId: string): Promise<void> =>
    open().then(
      (db) =>
        new Promise((resolve, reject) => {
          const transaction = db.transaction("calls", "readwrite")
          const index = transaction.objectStore("calls").index("envId")
          const request = index.openKeyCursor(IDBKeyRange.only(envId))
          request.onsuccess = () => {
            const cursor = request.result
            if (!cursor) return
            transaction.objectStore("calls").delete(cursor.primaryKey)
            cursor.continue()
          }
          transaction.oncomplete = () => resolve()
          transaction.onerror = () => reject(transaction.error)
        }),
    ),
}

export function newId(): string {
  return crypto.randomUUID()
}
