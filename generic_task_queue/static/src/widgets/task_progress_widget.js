/** @odoo-module **/

import { Component, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const TERMINAL_STATES = new Set(["done", "failed", "cancelled"]);

/**
 * TaskProgressWidget — live progress bar for generic.task.queue.task records.
 *
 * Reads id, state, and progress directly from the record — no options needed.
 * Subscribes to gtq_task_progress and gtq_task_update bus notifications for
 * the record's own id. Calls record.load() on terminal state so the final
 * progress value persists from the database.
 *
 * Usage (list and form views):
 *   <field name="progress" widget="gtq_task_progress"/>
 */
export class TaskProgressWidget extends Component {
    static template = "generic_task_queue.TaskProgressWidget";
    static props = {
        record: { type: Object },
        // Accept any extra props from either registry without validation errors.
        "*": true,
    };

    setup() {
        this.notifService = useService("gtq_task_notification");
        this._unsubscribe = null;

        this.state = useState({
            progress: this.props.record.data.progress || 0,
            taskState: this.props.record.data.state || null,
        });

        useEffect(
            () => {
                this._resubscribe();
            },
            () => [this.props.record.resId]
        );

        // Sync local state from the record whenever it reloads.
        // Bus notifications update state while the task runs; this
        // effect ensures the widget reflects DB values after
        // record.load() and on initial render with stale local state.
        useEffect(
            () => {
                this.state.taskState =
                    this.props.record.data.state || null;
                this.state.progress =
                    this.props.record.data.progress || 0;
            },
            () => [
                this.props.record.data.state,
                this.props.record.data.progress,
            ]
        );

        onWillUnmount(() => {
            this._unsubscribe?.();
        });
    }

    _resubscribe() {
        this._unsubscribe?.();
        this._unsubscribe = null;

        const taskId = this.props.record.resId;
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
            this.props.record.load();
        }
    }

    get isActive() {
        return (
            this.state.taskState !== null &&
            !TERMINAL_STATES.has(this.state.taskState)
        );
    }

    /**
     * When active: live value from bus notifications.
     * When inactive: DB value from the record (authoritative after
     * record.load()), falling back to state.progress to avoid showing
     * 0% in the brief window between the terminal notification and the
     * record reload completing.
     */
    get displayProgress() {
        return this.isActive
            ? this.state.progress
            : (this.props.record.data.progress || this.state.progress);
    }
}

registry.category("fields").add("gtq_task_progress", {
    component: TaskProgressWidget,
    supportedTypes: ["integer"],
});
