def f(db, user_id):
    user = db.get(user_id)
    if not user:
        return None
    return user
