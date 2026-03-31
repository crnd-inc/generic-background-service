{
    "name": "Generic Task Queue",
    "version": "18.0.0.0.1",
    "author": "Center of Research and Development",
    "website": "https://crnd.pro",
    "license": "LGPL-3",
    "summary": "Declarative background task queue for Odoo",
    "category": "Technical Settings",
    "depends": [
        "generic_background_service",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/generic_task_queue_task_views.xml",
    ],
    "images": ["static/description/banner.png"],
}
