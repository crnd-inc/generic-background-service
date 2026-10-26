{
    "name": "Generic Background Migration",
    "version": "20.0.0.1.4",
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
        "security/ir.access.csv",
        "views/generic_background_migration_views.xml",
    ],
}
