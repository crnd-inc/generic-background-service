{
    "name": "Generic Task Queue",
    "version": "18.0.0.0.6",
    "author": "Center of Research and Development",
    "website": "https://crnd.pro",
    "license": "LGPL-3",
    "summary": "Declarative background task queue for Odoo",
    "category": "Technical Settings",
    "depends": [
        "generic_background_service",
        "bus",
    ],
    "data": [
        "security/groups.xml",
        "security/ir.model.access.csv",
        "security/ir_rule.xml",
        "views/generic_task_queue_task_views.xml",
        "data/cron.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "generic_task_queue/static/src/services/"
            "gtq_task_notification_service.js",
            "generic_task_queue/static/src/fields/task_auto_refresh_field.js",
            "generic_task_queue/static/src/fields/task_auto_refresh_field.xml",
            "generic_task_queue/static/src/widgets/task_progress_widget.js",
            "generic_task_queue/static/src/widgets/task_progress_widget.xml",
        ],
    },
    "images": ["static/description/banner.png"],
}
