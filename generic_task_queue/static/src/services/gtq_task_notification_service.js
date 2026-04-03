/** @odoo-module **/

import { registry } from "@web/core/registry";

/**
 * Service that subscribes to gtq_task_progress and gtq_task_update
 * bus channels and dispatches notifications to registered callbacks
 * by task_id.
 *
 * Usage:
 *   const unsub = gtqTaskNotification.subscribe(taskId, (payload) => { ... });
 *   // call unsub() to unregister
 */
export const gtqTaskNotificationService = {
    dependencies: ["bus_service"],

    start(env, { bus_service }) {
        // Map of task_id (number) → Set of callback functions
        const listeners = new Map();

        function subscribe(task_id, callback) {
            if (!listeners.has(task_id)) {
                listeners.set(task_id, new Set());
            }
            listeners.get(task_id).add(callback);
            return function unsubscribe() {
                const cbs = listeners.get(task_id);
                if (cbs) {
                    cbs.delete(callback);
                    if (cbs.size === 0) {
                        listeners.delete(task_id);
                    }
                }
            };
        }

        function _dispatch(payload) {
            const cbs = listeners.get(payload.task_id);
            if (cbs) {
                cbs.forEach((cb) => cb(payload));
            }
        }

        bus_service.subscribe("gtq_task_update", _dispatch);
        bus_service.subscribe("gtq_task_progress", _dispatch);
        bus_service.start();

        return { subscribe };
    },
};

registry
    .category("services")
    .add("gtq_task_notification", gtqTaskNotificationService);
