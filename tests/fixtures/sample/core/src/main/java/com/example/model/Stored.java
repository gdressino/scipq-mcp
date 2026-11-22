package com.example.model;

import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;

/** A persistence marker for the fixture: an entity annotation with a table attribute. */
@Retention(RetentionPolicy.RUNTIME)
public @interface Stored {
    String table() default "";
    boolean cached() default false;
}
