import { useEffect, useState } from "react";
import { fetchSettings, saveSettings, type SettingsPayload } from "../api/client";

const TOKEN_KEY = "signals.adminToken";

function remembered(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

/**
 * The flag threshold: what gets pushed and price-stamped. Deliberately separate
 * from the score slider, which only changes what this viewer is looking at.
 */
export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [value, setValue] = useState(30);
  const [token, setToken] = useState(remembered);
  const [message, setMessage] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    fetchSettings()
      .then((s) => {
        setSettings(s);
        setValue(s.flag_threshold);
      })
      .catch((e) => setMessage({ tone: "err", text: String(e.message ?? e) }));
  }, []);

  const save = async () => {
    try {
      const next = await saveSettings(value, token);
      setSettings(next);
      try {
        sessionStorage.setItem(TOKEN_KEY, token);
      } catch {
        /* private window: the token just is not remembered */
      }
      setMessage({ tone: "ok", text: `Saved. The worker picks it up within 30 seconds.` });
    } catch (e) {
      setMessage({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div className="modal-back" onMouseDown={onClose}>
      <div className="modal" role="dialog" aria-label="Settings" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Settings</h2>
          <button className="x" onClick={onClose} aria-label="close">
            ×
          </button>
        </div>

        <h3>Flag threshold</h3>
        <p className="help">
          Events scoring at or above this are <b>pushed to the dashboard and price-stamped</b>.
          It is not the score slider — that only changes what you are looking at. Nothing is
          ever deleted: every event is stored at its score, so lowering this later surfaces
          what was always there.
        </p>

        <div className="thr">
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={value}
            disabled={!settings?.editable}
            onChange={(e) => setValue(Number(e.target.value))}
          />
          <span className="mono big">{value}</span>
        </div>

        {settings && !settings.editable && (
          <p className="help warn">
            Read-only: set <span className="mono">ADMIN_TOKEN</span> in <span className="mono">.env</span>{" "}
            and restart the API to allow changes.
          </p>
        )}

        {settings?.editable && (
          <>
            <label className="field">
              <span>Admin token</span>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="from ADMIN_TOKEN in .env"
                autoComplete="off"
              />
            </label>
            <div className="actions">
              <button
                className="primary"
                onClick={save}
                disabled={!token || value === settings.flag_threshold}
              >
                Save
              </button>
              <span className="mono cur">currently {settings.flag_threshold}</span>
            </div>
          </>
        )}
        {message && <p className={`help ${message.tone}`}>{message.text}</p>}
      </div>
    </div>
  );
}
