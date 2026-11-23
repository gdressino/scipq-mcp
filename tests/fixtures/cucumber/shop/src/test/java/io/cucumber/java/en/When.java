package io.cucumber.java.en;

import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;

/** Stand-in for Cucumber's annotation (the fixture compiles without its jar). */
@Retention(RetentionPolicy.RUNTIME)
public @interface When {
    String value();
}
