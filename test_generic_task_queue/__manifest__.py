{
    'name': "Generic Task Queue (Tests)",
    'summary': (
        "Technical module for testing Generic Task Queue"
    ),
    'author': "Center of Research and Development",
    'website': "https://crnd.pro",
    'category': 'Hidden',
    'version': '18.0.0.0.8',
    'depends': [
        'generic_task_queue',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/test_task_target_views.xml',
    ],
    'license': 'LGPL-3',
}
