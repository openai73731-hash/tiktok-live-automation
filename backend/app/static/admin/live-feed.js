/* Current-session merge shared by history rehydration and SSE. No delivery authority. */
globalThis.createLiveFeed = () => {
  let session = null, generation = 0;
  const rows = new Map();
  function select(id) {
    if (id !== session) { session = id; generation++; rows.clear(); }
  }
  function put(row) {
    if (row.live_session_id !== session || !session) return false;
    const old = rows.get(row.id);
    if (old?.suggestion && !row.suggestion) return false;
    rows.set(row.id, row);
    const ordered = [...rows.values()].sort((a,b) => new Date(b.received_at || b.timestamp) - new Date(a.received_at || a.timestamp));
    rows.clear(); ordered.slice(0,50).forEach(r => rows.set(r.id,r));
    return true;
  }
  return {rows, select, put, token: () => ({session, generation}),
    hydrate: (values, token) => {
      if (token.session !== session || token.generation !== generation) return;
      values.forEach(r => { if (!rows.has(r.id)) put(r); else if (!rows.get(r.id).suggestion && r.suggestion) put(r); });
    }};
};
