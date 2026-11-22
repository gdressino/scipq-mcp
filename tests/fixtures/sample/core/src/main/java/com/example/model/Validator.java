package com.example.model;

@FunctionalInterface
public interface Validator {
    boolean validate(Entity e);
}
