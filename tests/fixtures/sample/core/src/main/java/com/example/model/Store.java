package com.example.model;

/**
 * Opens {@link Session}s. Mentions {@code @Deprecated} in javadoc: not an annotation use.
 */
public class Store {
    private final Session shared = new Session();

    @Deprecated
    public Entity load(long id) {
        try (Session session = new Session()) {
            return session.find(id);
        }
    }

    public boolean isDeprecated() {
        // @Deprecated in a comment is not an annotation use either
        return Store.class.isAnnotationPresent(Deprecated.class);
    }
}
