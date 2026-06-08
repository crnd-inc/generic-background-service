{
    "name": "Generic Background Migration",
    "version": "18.0.0.1.2",
    "author": "Center of Research and Development",
    "website": "https://crnd.pro",
    "license": "LGPL-3",
    "summary": (
        "[EXPERIMENTAL] One-shot background data migrations "
        "via background tasks"
    ),
    "category": "Technical Settings",
    "depends": [
        "generic_task_queue",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/generic_background_migration_views.xml",
    ],
}
