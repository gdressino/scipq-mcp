package com.vaadin.flow.router;

public @interface Route {
    String value() default "";

    Class<?> layout() default Object.class;
}
