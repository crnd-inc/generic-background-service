/** @odoo-module **/

import { Component, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";

const TERMINAL_STATES = new Set(["done", "failed", "cancelled"]);

/**
 * Module-level WeakMap for deduplicating record.load() calls.
 *
 * Multiple widgets on the same record (same form or same list row)
 * may receive terminal notifications simultaneously. The WeakMap
 * ensures only one reload is scheduled per record per event-loop turn.
 * Using WeakMap means records are garbage-collected normally when
 * the view closes.
 */
const _pendingReloads = new WeakMap();

function scheduleReload(record) {
    if (_pendingReloads.has(record)) {
        return;
    }
    const timer = setTimeout(() => {
        _pendingReloads.delete(record);
        record.load();
    }, 0);
    _pendingReloads.set(record, timer);
}

/**
 * TaskAutoRefreshField — field widget for consumer records that reference a task.
 *
 * Place on a Many2one (or integer) field that holds a generic.task.queue.task id.
 * Subscribes to gtq_task_progress and gtq_task_update bus notifications for that
 * task. Shows a spinner and live progress bar while the task is active. Triggers
 * a deduplicated record.load() when the task reaches a terminal state so the
 * consumer record refreshes.
 *
 * Renders nothing when the task is inactive — the consumer's own UI takes over.
 *
 * Options:
 *   show_progress (bool, default true)  — show spinner + progress bar while active.
 *   label         (string, optional)    — label above the progress block. Defaults
 *                                         to the task display name (from M2O data).
 *                                         Pass "" to suppress. NOTE: a literal here
 *                                         is NOT translatable — prefer label_field.
 *   label_field    (string, optional)   — field name on this record holding the
 *                                         label text. Use this for a translatable
 *                                         label (the field value is computed
 *                                         server-side via env._()). Takes effect
 *                                         only when the `label` option is not set.
 *   state_field    (string, optional)   — field name on this record holding the
 *                                         task's current state. Used for cold-start:
 *                                         shows the spinner immediately on open if
 *                                         the task is already running.
 *   progress_field (string, optional)   — field name on this record holding the
 *                                         task's current progress (0-100). Used for
 *                                         cold-start: shows the correct bar position
 *                                         immediately on open mid-run.
 */
export class TaskAutoRefreshField extends Component {
    static template = "generic_task_queue.TaskAutoRefreshField";
    static props = {
        ...standardFieldProps,
        showProgress: { type: Boolean, optional: true },
        label: { type: String, optional: true },
        labelField: { type: String, optional: true },
        stateField: { type: String, optional: true },
        progressField: { type: String, optional: true },
    };

    setup() {
        this.notifService = useService("gtq_task_notification");
        this._unsubscribe = null;

        const initialState = this.props.stateField
            ? (this.props.record.data[this.props.stateField] ?? null)
            : null;

        const initialProgress = this.props.progressField
            ? (this.props.record.data[this.props.progressField] || 0)
            : 0;

        this.state = useState({
            progress: initialProgress,
            taskState: initialState,
        });

        useEffect(
            () => {
                this._resubscribe();
            },
            () => [this._currentTaskId()]
        );

        // Sync taskState and progress from related fields whenever the
        // record reloads. Without this, isActive and the bar position
        // stay stale after record.load() if the corresponding bus
        // notification was not received.
        useEffect(
            () => {
                if (this.props.stateField) {
                    this.state.taskState =
                        this.props.record.data[this.props.stateField]
                        ?? null;
                }
            },
            () => [
                this.props.stateField
                    ? this.props.record.data[this.props.stateField]
                    : null,
            ]
        );

        useEffect(
            () => {
                if (this.props.progressField) {
                    this.state.progress =
                        this.props.record.data[this.props.progressField]
                        || 0;
                }
            },
            () => [
                this.props.progressField
                    ? this.props.record.data[this.props.progressField]
                    : null,
            ]
        );

        onWillUnmount(() => {
            this._unsubscribe?.();
        });
    }

    _currentTaskId() {
        const value = this.props.record.data[this.props.name];
        if (!value) {
            return null;
        }
        // Many2one returns [id, name]; integer fields return a number.
        return Array.isArray(value) ? value[0] : value;
    }

    _resubscribe() {
        this._unsubscribe?.();
        this._unsubscribe = null;

        const taskId = this._currentTaskId();
        if (!taskId) {
            return;
        }

        this._unsubscribe = this.notifService.subscribe(
            taskId,
            (payload) => this._onNotification(payload)
        );
    }

    onClickReload() {
        this.props.record.load();
    }

    _onNotification(payload) {
        if (payload.progress !== undefined) {
            this.state.progress = payload.progress;
        }
        if (payload.state !== undefined) {
            this.state.taskState = payload.state;
        }
        if (TERMINAL_STATES.has(payload.state)) {
            scheduleReload(this.props.record);
        }
    }

    get isActive() {
        return (
            this.state.taskState !== null &&
            !TERMINAL_STATES.has(this.state.taskState)
        );
    }

    /**
     * Label above the progress block.
     *
     * Explicit label option takes precedence (empty string suppresses it),
     * then label_field (read from this record — use for a translatable label),
     * then the task display name from M2O data.
     */
    get displayLabel() {
        if (this.props.label !== undefined) {
            return this.props.label;
        }
        if (this.props.labelField) {
            const fieldValue = this.props.record.data[this.props.labelField];
            if (fieldValue) {
                return fieldValue;
            }
        }
        const value = this.props.record.data[this.props.name];
        return Array.isArray(value) ? value[1] : null;
    }
}

export const taskAutoRefreshField = {
    component: TaskAutoRefreshField,
    supportedTypes: ["many2one", "integer"],
    extractProps({ options }) {
        return {
            showProgress: options.show_progress !== false,
            label: options.label,
            labelField: options.label_field || null,
            stateField: options.state_field || null,
            progressField: options.progress_field || null,
        };
    },
};

registry.category("fields").add("gtq_task_auto_refresh", taskAutoRefreshField);
