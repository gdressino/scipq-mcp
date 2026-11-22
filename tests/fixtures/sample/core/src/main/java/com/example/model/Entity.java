package com.example.model;

/**
 * Base class for every persisted entity.
 */
public abstract class Entity implements Identifiable {
    private int state;

    /**
     * Changes the lifecycle state.
     *
     * @param state the new state
     */
    public void setState(int state) {
        this.state = state;
    }

    public int getState() {
        return state;
    }

    public static Entity findById(long id) {
        return null;
    }

    @Override
    public abstract String uid();
}
