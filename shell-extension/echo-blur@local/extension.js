/*
 * Echo Blur - applies GNOME Shell's own blur-behind-window effect
 * (Shell.BlurEffect, the same mechanism the Shell uses internally for the
 * Overview) specifically to the Echo window, matched by app ID or
 * window class. This is what makes the frosted-glass theme in the app
 * actually blur your desktop instead of just showing a tinted color.
 *
 * Honest caveat: Shell.BlurEffect's exact API (property names, enum values)
 * has shifted a little across GNOME releases. This targets the modern ESM
 * extension format (GNOME 45+) and the API shape as of GNOME 45-48. If it
 * doesn't apply cleanly on your GNOME 50, check `journalctl --user -f` for
 * a `[echo-blur]` error while re-enabling the extension - that'll show
 * exactly which property/enum needs updating, and the fix is almost always
 * a one-line property rename.
 */
import Shell from 'gi://Shell';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const TARGET_APP_ID = 'io.echo.Player';

export default class EchoBlurExtension extends Extension {
    enable() {
        this._blurredActors = new Map();

        this._windowCreatedId = global.display.connect('window-created', (_display, metaWindow) => {
            this._maybeBlur(metaWindow);
        });

        // catch windows that already exist when the extension is (re-)enabled
        global.get_window_actors().forEach(actor => {
            this._maybeBlur(actor.get_meta_window());
        });
    }

    disable() {
        if (this._windowCreatedId) {
            global.display.disconnect(this._windowCreatedId);
            this._windowCreatedId = null;
        }
        if (this._blurredActors) {
            for (const [metaWindow, effect] of this._blurredActors) {
                const actor = metaWindow.get_compositor_private();
                if (actor && effect) {
                    try {
                        actor.remove_effect(effect);
                    } catch (e) {
                        // window may already be gone; nothing to clean up
                    }
                }
            }
            this._blurredActors.clear();
        }
        this._blurredActors = null;
    }

    _isEchoWindow(metaWindow) {
        if (!metaWindow) return false;
        try {
            const appId = metaWindow.get_gtk_application_id?.();
            if (appId === TARGET_APP_ID) return true;
        } catch (e) { /* not all Meta.Window objects expose this */ }
        const wmClass = metaWindow.get_wm_class();
        if (wmClass && wmClass.toLowerCase().includes('echo')) return true;
        return false;
    }

    _maybeBlur(metaWindow) {
        if (!this._isEchoWindow(metaWindow)) return;

        const applyBlur = () => {
            if (this._blurredActors.has(metaWindow)) return;
            const actor = metaWindow.get_compositor_private();
            if (!actor) return;
            try {
                const effect = new Shell.BlurEffect({
                    brightness: 0.75,
                    sigma: 48,
                    mode: Shell.BlurMode.BACKGROUND,
                });
                actor.add_effect_with_name('echo-blur', effect);
                this._blurredActors.set(metaWindow, effect);
            } catch (e) {
                logError(e, '[echo-blur] failed to apply Shell.BlurEffect');
            }
        };

        const actor = metaWindow.get_compositor_private();
        if (actor) {
            applyBlur();
        } else {
            const shownId = metaWindow.connect('shown', () => {
                metaWindow.disconnect(shownId);
                applyBlur();
            });
        }

        const unmanagedId = metaWindow.connect('unmanaged', () => {
            metaWindow.disconnect(unmanagedId);
            if (this._blurredActors) this._blurredActors.delete(metaWindow);
        });
    }
}
