#!/usr/bin/env bash
set -euo pipefail

JSON_FILE=$1
GRADLE_DIR=~/.gradle
INIT_GRADLE=$GRADLE_DIR/init.gradle

# Exit early if there are no dependencies
DEP_COUNT=$(jq '.dependencies | length' "$JSON_FILE")
if [ "$DEP_COUNT" -eq 0 ]; then
    echo "No dependencies found, failure was probably not due to a required dependency. Failing to propagate initial failure."
    exit 1
fi

# Write init.gradle header to user gradle dir 
# (first as a temp file since it will parsed w. just this)
mkdir -p $GRADLE_DIR
cat > $INIT_GRADLE.tmp << 'EOF'
allprojects {
    repositories {
        mavenLocal()
    }
    configurations.all {
        resolutionStrategy.eachDependency { details ->
EOF

# Process each dependency
jq -c '.dependencies[]' $JSON_FILE | while read -r dep; do
    JAR_PATH=$(echo $dep | jq -r '.jar_path')
    REPO_URL=$(echo $dep | jq -r '.repo_url')
    COMMIT_SHA=$(echo $dep | jq -r '.commit_sha')

    PREV_DIR=$(pwd)
    REPO_NAME=$(basename $REPO_URL .git)
    WORK_DIR=$(mktemp -d)/$REPO_NAME
    mkdir -p $WORK_DIR
    echo "Processing $REPO_URL @ $COMMIT_SHA (in $WORK_DIR)"
    cd $WORK_DIR

    # Shallow clone and checkout
    git init .
    git remote add origin $REPO_URL
    git fetch --depth 1 origin $COMMIT_SHA
    git fetch --tags --depth 1
    git checkout $COMMIT_SHA

    # Get coordinates
    PROPS=$(./gradlew -q :properties 2>/dev/null)
    GROUP=$(echo "$PROPS" | grep '^group:' | awk '{print $2}')
    # Gradle does not actually seem to have project name, 
    # it just takes the project folder name (in this case, the repo)
    PROJECT=$REPO_NAME
    # We don't actually care about the version, we just want to override it (this just returns a short sha rn)
    # If we ever do care about it, we need to figure out how to get it with just a shallow clone
    VERSION=$(echo "$PROPS" | grep '^version:' | awk '{print $2}')-local

    echo "Coordinates of local maven result: $GROUP:$PROJECT:$VERSION"

    # Generate a pom for local maven (need to do this, so we have deps included)
    VERSION=$VERSION ./gradlew generatePomFileForMavenPublication

    # Setup local maven
    MAVEN_PATH="${GROUP//.//}/$PROJECT/$VERSION"
    mkdir -p ~/.m2/repository/$MAVEN_PATH

    # Copy in pom and create fake normal jar so gradle is happy
    mv ./build/publications/maven/pom-default.xml ~/.m2/repository/$MAVEN_PATH/$PROJECT-$VERSION.pom
    touch ~/.m2/repository/$MAVEN_PATH/$PROJECT-$VERSION.jar

    # Go back to original dir & clean up workdir
    cd $PREV_DIR
    rm -rf $WORK_DIR

    # Move actual jar to local maven
    cp $JAR_PATH ~/.m2/repository/$MAVEN_PATH/$PROJECT-$VERSION-dev.jar

    # Append override to init.gradle
    cat >> $INIT_GRADLE.tmp << EOF
            if (details.requested.module.toString() == '${GROUP}:${PROJECT}') {
                details.useVersion '${VERSION}'
                details.because 'PR dependency override'
            }
EOF

    rm -rf $WORK_DIR
done

# Close init.gradle
cat >> $INIT_GRADLE.tmp << 'EOF'
        }
    }
}
EOF

# Rename to actual now that its parseable
mv $INIT_GRADLE.tmp $INIT_GRADLE