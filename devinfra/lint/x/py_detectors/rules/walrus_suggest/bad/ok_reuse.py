def f(db, user_id):
    # Assignment reused later; walrus can both test and bind
    user = db.get(user_id)
    if user is None:
        return None
    return user.name if user.name else user
