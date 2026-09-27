def rows_by_title(env):
    return {r.title: r for r in env.controller.rows()}
